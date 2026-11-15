#!/usr/bin/env python3
"""my_immich — a small local web GUI (with a macOS menu-bar icon) around immich-go.

Run:   python3 my_immich.py            (page only)
       python3 my_immich.py --tray     (menu-bar icon too; needs `rumps`)
Then use the page that opens in your browser (http://127.0.0.1:8765).

- Detects camera cards (any mounted volume with a DCIM folder) and lets you
  pick one, or browse to any other folder.
- Shows every import setting, editable, and saves them to
  ~/.config/my_immich/config.json (the API key is stored there too, so the
  file is created readable by you only).
- Runs `immich-go upload from-folder` itself and streams its log to the page.

Python 3.8+, standard library only. Needs immich-go (brew install immich-go).
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "my_immich"
APP_ID = "my_immich"
CONFIG_DIR = Path(os.environ.get("MY_IMMICH_CONFIG_DIR", Path.home() / ".config" / "my_immich"))
CONFIG_FILE = CONFIG_DIR / "config.json"
VOLUMES_ROOT = Path(os.environ.get("MY_IMMICH_VOLUMES", "/Volumes"))
MENUBAR_ICON = Path(__file__).resolve().parent / "assets" / "menubar.png"
OLD_SCRIPT_CANDIDATES = [
    Path.home() / "scripts" / "sd-to-immich.sh",
    Path(__file__).resolve().parent / "sd-to-immich.sh",
]
MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".gif", ".avif",
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".rw2", ".orf",
    ".dng", ".pef", ".raw", ".3fr", ".iiq", ".x3f", ".srw",
    ".mp4", ".mov", ".m4v", ".avi", ".mts", ".m2ts", ".mkv", ".3gp", ".insv", ".webm",
}

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mts", ".m2ts", ".mkv", ".3gp", ".insv", ".webm"}
RAW_EXT = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".rw2", ".orf", ".dng",
           ".pef", ".raw", ".3fr", ".iiq", ".x3f", ".srw"}
BROWSER_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}      # a browser can show these as they are
SAMPLE_COUNT = 12                                             # thumbnails on the contact sheet
# immich-go --no-ui rewrites one status line twice a second:
PROGRESS_RE = re.compile(r"Immich read (\d+)%, Assets found: (\d+), Upload errors: (\d+), Uploaded (\d+)")

RAW_JPEG = ["NoStack", "KeepRaw", "KeepJPG", "StackCoverRaw", "StackCoverJPG"]
BURST = ["NoStack", "Stack", "StackKeepRaw", "StackKeepJPEG"]
HEIC_JPEG = ["NoStack", "KeepHeic", "KeepJPG", "StackCoverHeic", "StackCoverJPG"]
FOLDER_ALBUM = ["NONE", "FOLDER", "PATH"]
INCLUDE_TYPE = ["all", "IMAGE", "VIDEO"]
ON_ERRORS = ["stop", "continue"]

DEFAULTS = {
    "server": "",
    "api_key": "",
    "immich_go_path": "",
    "raw_jpeg": "KeepRaw",
    "burst": "NoStack",
    "heic_jpeg": "NoStack",
    "session_tag": True,
    "pause_jobs": True,
    "on_errors": "stop",
    "folder_as_album": "NONE",
    "into_album": "",
    "include_type": "all",
    "recursive": True,
    "keep_awake": True,
    "eject_after": False,
    "extra_args": "",
    "last_folder": "",
    "recent_folders": [],
}
MAX_RECENT = 6
ENUMS = {
    "raw_jpeg": RAW_JPEG, "burst": BURST, "heic_jpeg": HEIC_JPEG,
    "folder_as_album": FOLDER_ALBUM, "include_type": INCLUDE_TYPE, "on_errors": ON_ERRORS,
}


# ----------------------------------------------------------------- config ---

def import_old_script():
    """Pull settings out of the earlier sd-to-immich.sh, if it is around."""
    found = {}
    for path in OLD_SCRIPT_CANDIDATES:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue

        def grab(var):
            # VAR="${VAR:-value}"  or  VAR="value"
            m = re.search(r'^%s="\$\{%s:-([^}]*)\}"' % (var, var), text, re.M)
            if not m:
                m = re.search(r'^%s="([^"$]*)"' % var, text, re.M)
            return m.group(1).strip() if m else None

        server, key = grab("IMMICH_SERVER"), grab("API_KEY")
        raw, tag, pause = grab("RAW_JPEG_MODE"), grab("SESSION_TAG"), grab("PAUSE_JOBS")
        if server:
            found["server"] = server
        if key:
            found["api_key"] = key
        if raw in RAW_JPEG:
            found["raw_jpeg"] = raw
        if tag in ("true", "false"):
            found["session_tag"] = tag == "true"
        if pause in ("true", "false"):
            found["pause_jobs"] = pause == "true"
        if found:
            found["_imported_from"] = str(path)
            break
    return found


def clean_config(data):
    cfg = dict(DEFAULTS, recent_folders=[])
    for key, default in DEFAULTS.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(default, list):
            if isinstance(value, list):
                cfg[key] = [str(v) for v in value if isinstance(v, str) and v][:MAX_RECENT]
            continue
        if isinstance(default, bool):
            cfg[key] = bool(value)
        else:
            value = str(value).strip()
            if key in ENUMS and value not in ENUMS[key]:
                # on_errors may also be a number of tolerated errors
                if not (key == "on_errors" and value.isdigit()):
                    continue
            cfg[key] = value
    cfg["server"] = cfg["server"].rstrip("/")
    return cfg


def load_config():
    imported_from = None
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            data = {}
    else:
        data = import_old_script()
        imported_from = data.pop("_imported_from", None)
    cfg = clean_config(data)
    if imported_from:
        save_config(cfg)
    return cfg, imported_from


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(str(tmp), str(CONFIG_FILE))
    try:
        os.chmod(str(CONFIG_FILE), 0o600)
    except OSError:
        pass


# ------------------------------------------------------------ environment ---

def find_immich_go(cfg):
    if cfg.get("immich_go_path"):
        p = Path(cfg["immich_go_path"]).expanduser()
        return str(p) if p.is_file() and os.access(str(p), os.X_OK) else None
    hit = shutil.which("immich-go")
    if hit:
        return hit
    for cand in ("/opt/homebrew/bin/immich-go", "/usr/local/bin/immich-go",
                 str(Path.home() / ".homebrew" / "bin" / "immich-go"),
                 str(Path.home() / "go" / "bin" / "immich-go")):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def list_cards():
    """Mounted volumes that look like camera cards (have a DCIM folder)."""
    cards = []
    try:
        entries = sorted(VOLUMES_ROOT.iterdir())
    except OSError:
        return cards
    for vol in entries:
        dcim = vol / "DCIM"
        try:
            if not dcim.is_dir():
                continue
            subs = sorted(d.name for d in dcim.iterdir() if d.is_dir() and not d.name.startswith("."))
        except OSError:
            continue
        cards.append({"name": vol.name, "volume": str(vol), "path": str(dcim), "folders": subs})
    return cards


def list_drives():
    """Other mounted volumes (USB sticks, external disks): everything under the
    volumes root that is neither the startup disk nor a camera card."""
    drives = []
    try:
        entries = sorted(VOLUMES_ROOT.iterdir(), key=lambda v: v.name.lower())
    except OSError:
        return drives
    for vol in entries:
        try:
            if vol.name.startswith(".") or not vol.is_dir():
                continue
            if os.path.realpath(str(vol)) == "/":        # the startup disk
                continue
            if (vol / "DCIM").is_dir():                  # listed as a card instead
                continue
        except OSError:
            continue
        drives.append({"name": vol.name, "path": str(vol)})
    return drives


def media_here(path, limit=5000):
    """How many media files sit directly in a folder (not in its subfolders)."""
    count = seen = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                seen += 1
                if seen > limit:
                    break
                name = entry.name
                if not name.startswith(".") and os.path.splitext(name)[1].lower() in MEDIA_EXT:
                    count += 1
    except OSError:
        return None
    return count


def walk_media(path):
    """Yield (full path, extension, stat) for every media file under a folder."""
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if name.startswith(".") or ext not in MEDIA_EXT:
                continue
            full = os.path.join(root, name)
            try:
                yield full, ext, os.stat(full)
            except OSError:
                continue


def scan_folder(path, want_samples=False):
    """Count what is in a folder: files, bytes, kinds, date range and (optionally)
    an evenly spread sample of photos for the contact sheet."""
    count = size = videos = raws = 0
    oldest = newest = None
    photos = []
    for full, ext, st in walk_media(path):
        count += 1
        size += st.st_size
        if ext in VIDEO_EXT:
            videos += 1
        else:
            if ext in RAW_EXT:
                raws += 1
            if want_samples:
                photos.append(full)
        oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
        newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
    out = {"files": count, "bytes": size, "videos": videos, "photos": count - videos, "raw": raws,
           "oldest": oldest, "newest": newest}
    if want_samples:
        n = len(photos)
        picks = [photos[(i * n) // SAMPLE_COUNT] for i in range(SAMPLE_COUNT)] if n > SAMPLE_COUNT else photos
        out["samples"] = picks
    return out


THUMB_DIR = None
THUMB_LOCK = threading.Semaphore(2)


def make_thumb(path):
    """A small JPEG (or the file itself) for the contact sheet, or None.

    macOS's `sips` reads camera RAW files, so it does the work there. Without it,
    only formats a browser can already show are served, and only if they are small.
    """
    global THUMB_DIR
    ext = os.path.splitext(path)[1].lower()
    if ext not in MEDIA_EXT or ext in VIDEO_EXT or not os.path.isfile(path):
        return None
    sips = shutil.which("sips")
    if sips:
        if THUMB_DIR is None:
            THUMB_DIR = tempfile.mkdtemp(prefix="my_immich_thumbs_")
        out = os.path.join(THUMB_DIR, hashlib.sha1(path.encode()).hexdigest() + ".jpg")
        if not os.path.isfile(out):
            with THUMB_LOCK:
                try:
                    subprocess.run([sips, "-s", "format", "jpeg", "-Z", "360", path, "--out", out],
                                   capture_output=True, timeout=30)
                except (OSError, subprocess.SubprocessError):
                    return None
        return out if os.path.isfile(out) and os.path.getsize(out) > 0 else None
    if ext in BROWSER_EXT and os.path.getsize(path) <= 12 * 1024 * 1024:
        return path
    return None


def places():
    home = Path.home()
    out = [{"label": "Home", "path": str(home), "kind": "home"}]
    for name in ("Pictures", "Desktop", "Downloads", "Documents", "Movies"):
        if (home / name).is_dir():
            out.append({"label": name, "path": str(home / name), "kind": "folder"})
    for card in list_cards():
        out.append({"label": card["name"], "path": card["volume"], "kind": "card"})
    for drive in list_drives():
        out.append({"label": drive["name"], "path": drive["path"], "kind": "drive"})
    return out


def browse(path):
    """Folder listing for the folder chooser."""
    error = None
    p = Path(path).expanduser() if path else Path.home()
    try:
        if not p.is_dir():
            error = "That folder does not exist: %s" % p
            p = Path.home()
        p = p.resolve()
    except OSError as exc:
        error, p = str(exc), Path.home()
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    dirs.append(child.name)
            except OSError:
                pass
    except PermissionError:
        error = ("macOS did not allow reading this folder. Allow my_immich (Python) under "
                 "System Settings, Privacy & Security, Files and Folders, then try again.")
    except OSError as exc:
        error = str(exc)
    crumbs, parts = [], p.parts
    for i in range(len(parts)):
        crumbs.append({"label": parts[i] if i else "Computer", "path": str(Path(*parts[:i + 1]))})
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs,
            "crumbs": crumbs, "media_here": media_here(str(p)), "places": places(), "error": error}


# Sent with every request to Immich. Python's default "Python-urllib/3.x" is on
# the block list of Cloudflare and several reverse proxies, which answer 403
# before the request ever reaches Immich; a named client passes.
USER_AGENT = "my_immich (immich-go front end)"


def immich_get(server, path, api_key="", timeout=10):
    """GET a JSON document from the Immich API."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(server + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def http_error_detail(exc):
    """What an HTTP error page says, shortened: Immich's JSON message, or the
    first line of a proxy's page (Cloudflare writes "error code: 1010")."""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    try:
        return str(json.loads(body).get("message", ""))[:200]
    except Exception:  # noqa: BLE001
        pass
    text = re.sub(r"<[^>]+>", " ", body)
    return " ".join(text.split())[:120]


def test_connection(cfg):
    server = cfg["server"]
    if not server:
        return {"ok": False, "message": "Enter the Immich server address first."}
    try:
        immich_get(server, "/api/server/ping")
    except urllib.error.HTTPError as exc:
        # Something answered, so the network is fine: say what it said.
        detail = http_error_detail(exc)
        if exc.code in (401, 403):
            hint = ("A proxy or firewall in front of Immich (Cloudflare, an access gateway, a "
                    "reverse proxy rule) is refusing this app before the request reaches Immich.")
        elif exc.code == 404:
            hint = "That address answers, but it does not look like Immich. Check the address and port."
        else:
            hint = "The server or a proxy in front of it returned an error."
        return {"ok": False, "message": "%s answered HTTP %s %s. %s%s"
                % (server, exc.code, exc.reason, hint, (" (%s)" % detail) if detail else "")}
    except Exception as exc:  # noqa: BLE001 - shown to the user as-is
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "message": "Cannot reach Immich at %s. Check the address and that this Mac is on "
                                        "the same network. (%s)" % (server, reason)}
    if not cfg["api_key"]:
        return {"ok": False, "message": "Server is reachable, but no API key is set."}
    try:
        me = immich_get(server, "/api/users/me", cfg["api_key"])
    except urllib.error.HTTPError as exc:
        return {"ok": False, "message": "Server reachable, but the API key was refused (HTTP %s). %s"
                % (exc.code, http_error_detail(exc))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "Server reachable, but the key check failed (%s)." % exc}
    return {"ok": True, "message": "Connected as %s." % (me.get("name") or me.get("email") or "user")}


# -------------------------------------------------------------------- job ---

def build_command(cfg, source, dry_run, immich_go):
    cmd = [immich_go, "upload", "from-folder",
           "--server=" + cfg["server"],
           "--api-key=" + cfg["api_key"],
           "--no-ui",
           "--pause-immich-jobs=" + ("true" if cfg["pause_jobs"] else "false"),
           "--on-errors=" + cfg["on_errors"]]
    if cfg["raw_jpeg"] != "NoStack":
        cmd.append("--manage-raw-jpeg=" + cfg["raw_jpeg"])
    if cfg["burst"] != "NoStack":
        cmd.append("--manage-burst=" + cfg["burst"])
    if cfg["heic_jpeg"] != "NoStack":
        cmd.append("--manage-heic-jpeg=" + cfg["heic_jpeg"])
    if cfg["session_tag"]:
        cmd.append("--session-tag")
    if cfg["folder_as_album"] != "NONE":
        cmd.append("--folder-as-album=" + cfg["folder_as_album"])
    if cfg["into_album"]:
        cmd.append("--into-album=" + cfg["into_album"])
    if cfg["include_type"] != "all":
        cmd.append("--include-type=" + cfg["include_type"])
    if not cfg["recursive"]:
        cmd.append("--recursive=false")
    if dry_run:
        cmd.append("--dry-run")
    if cfg["extra_args"]:
        cmd.extend(shlex.split(cfg["extra_args"]))
    cmd.append(source)
    return cmd


def mask(text, key):
    return text.replace(key, "••••••••") if key else text


# What immich-go writes to its log for each thing it does to a file. The key is
# the text that appears in the log line; the value is the counter it feeds.
# From https://github.com/simulot/immich-go/blob/main/internal/fileevent/fileevents.go
EVENTS = {
    "discovered image": "found_images", "discovered video": "found_videos",
    "discovered sidecar": "sidecars", "discovered metadata": "sidecars",
    "discovered unknown file": "ignored", "discovered banned file": "ignored",
    "discovered unsupported file": "ignored",
    "uploaded successfully": "uploaded", "server asset upgraded": "upgraded",
    "metadata updated": "metadata", "server has duplicate": "on_server",
    "discarded server better": "on_server", "discarded local duplicate": "local_dupes",
    "discarded banned": "ignored", "discarded unsupported": "ignored",
    "discarded filtered": "set_aside", "discarded not selected": "set_aside",
    "upload failed": "errors", "server error": "errors", "file access error": "errors",
    "incomplete processing": "errors", "stacked": "stacked", "added to album": "albums",
    "tagged": "tagged", "live photo": "live_photos",
}
EVENT_RE = re.compile("(" + "|".join(re.escape(k) for k in sorted(EVENTS, key=len, reverse=True)) + ")")
FILE_ATTR_RE = re.compile(r'file=(?:"([^"]+)"|(\S+))')
FEED_KINDS = {"uploaded", "upgraded", "on_server", "local_dupes", "set_aside", "errors", "stacked"}


def net_counters(interface=None):
    """Bytes sent and received by this computer's network interface(s) since boot,
    as (sent, received), or None when they cannot be read."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["netstat", "-ibn"], capture_output=True, text=True, timeout=5).stdout
            sent = recv = 0
            seen = set()
            for line in out.splitlines()[1:]:
                parts = line.split()
                # one "<Link#N>" row per interface carries its totals; columns are counted
                # from the right because the address column is sometimes empty
                if len(parts) < 10 or not parts[2].startswith("<Link#") or parts[0] in seen:
                    continue
                name = parts[0].rstrip("*")
                if name.startswith("lo") or (interface and name != interface):
                    continue
                seen.add(parts[0])
                sent += int(parts[-2])
                recv += int(parts[-5])
            return (sent, recv) if seen else None
        with open("/proc/net/dev") as fh:
            sent = recv = 0
            found = False
            for line in fh.read().splitlines()[2:]:
                name, _, rest = line.partition(":")
                name, cols = name.strip(), rest.split()
                if name == "lo" or len(cols) < 9 or (interface and name != interface):
                    continue
                found = True
                recv += int(cols[0])
                sent += int(cols[8])
            return (sent, recv) if found else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def route_interface(host):
    """The network interface macOS would use to reach a host (for example en0)."""
    if sys.platform != "darwin" or not host:
        return None
    try:
        out = subprocess.run(["route", "-n", "get", host], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"interface:\s*(\S+)", out)
    return m.group(1) if m else None


def process_stats(pid):
    """CPU percent and memory of a process and its children (immich-go may sit
    under caffeinate), as {"cpu": float, "mem": bytes}, or None."""
    try:
        kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5).stdout.split()
        out = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", ",".join([str(pid)] + kids)],
                             capture_output=True, text=True, timeout=5).stdout
        cpu = mem = 0.0
        rows = 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                cpu += float(parts[0])
                mem += float(parts[1]) * 1024
                rows += 1
        return {"cpu": round(cpu, 1), "mem": int(mem)} if rows else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def server_stats(cfg):
    """A few numbers from the Immich server itself. Every part is optional: a key
    without the permission, or a server that answers differently, just leaves it out."""
    out = {}

    def get(path, auth=True):
        return immich_get(cfg["server"], path, cfg["api_key"] if auth else "", timeout=8)

    try:
        t0 = time.time()
        get("/api/server/ping", auth=False)
        out["ping_ms"] = round((time.time() - t0) * 1000)
    except Exception:  # noqa: BLE001
        return out
    try:
        stats = get("/api/assets/statistics")
        out["assets"] = int(stats.get("total", stats.get("images", 0) + stats.get("videos", 0)))
    except Exception:  # noqa: BLE001
        pass
    try:
        disk = get("/api/server/storage")
        if "diskUseRaw" in disk and "diskSizeRaw" in disk:
            out["disk_used"], out["disk_size"] = int(disk["diskUseRaw"]), int(disk["diskSizeRaw"])
    except Exception:  # noqa: BLE001
        pass
    try:
        v = get("/api/server/version", auth=False)
        out["version"] = "%s.%s.%s" % (v.get("major"), v.get("minor"), v.get("patch"))
    except Exception:  # noqa: BLE001
        pass
    return out


class Job:
    MAX_LINES = 20000
    MAX_POINTS = 240         # one sample a second: four minutes of chart

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.lines = []
        self.offset = 0          # lines dropped from the front
        self.running = False
        self.returncode = None
        self.source = ""
        self.dry_run = False
        self.started = None
        self.finished = None
        self.stopped = False
        self.note = ""
        self._reset_numbers()

    def _reset_numbers(self):
        self.progress = None     # latest numbers from immich-go's status line
        self.history = []        # (time, uploaded) points for the time-left estimate
        self.totals = None       # our own count of the source folder
        self.samples = []        # photos shown on the contact sheet
        self.current = ""        # last file immich-go reported
        self.counters = {}       # how many of each event immich-go logged
        self.feed = []           # the most recent events, newest last
        self.bytes_up = 0        # size of the files uploaded so far
        self.points = []         # per-second samples for the charts
        self.proc_stats = None
        self.server = {}         # numbers from the Immich server
        self.server_start = {}
        self.interface = None
        self.log_path = None
        self.log_events = False  # True once events arrive from immich-go's log file

    def add(self, line):
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > self.MAX_LINES:
                drop = len(self.lines) - self.MAX_LINES
                del self.lines[:drop]
                self.offset += drop

    def start(self, cfg, source, dry_run):
        with self.lock:
            if self.running:
                return "An import is already running."
        immich_go = find_immich_go(cfg)
        if not immich_go:
            return "immich-go was not found. Install it (brew install immich-go) or set its path in Settings."
        if not cfg["server"] or not cfg["api_key"]:
            return "Set the Immich server address and API key in Settings first."
        if not source or not os.path.isdir(source):
            return "The source folder does not exist: %s" % (source or "(none)")
        try:
            cmd = build_command(cfg, source, dry_run, immich_go)
        except ValueError as exc:
            return "Extra arguments could not be read: %s" % exc
        with self.lock:
            self.lines, self.offset = [], 0
            self.running, self.returncode, self.stopped = True, None, False
            self.source, self.dry_run = source, dry_run
            self.started, self.finished, self.note = time.time(), None, ""
            self._reset_numbers()
        threading.Thread(target=self._count_source, args=(source,), daemon=True).start()
        threading.Thread(target=self._run, args=(cfg, cmd), daemon=True).start()
        return None

    # -- the import itself
    def _run(self, cfg, cmd):
        key = cfg["api_key"]
        # Ask immich-go to write its detailed log to a file we can follow; that is
        # where the per-file events are. If this build does not know the flag,
        # run once more without it.
        want_log = "--log-file" not in cfg["extra_args"]
        code = -1
        for attempt in (1, 2):
            full = list(cmd)
            log_path = None
            if want_log:
                log_path = os.path.join(tempfile.gettempdir(), "my_immich-%d.log" % int(time.time() * 1000))
                full.insert(len(full) - 1, "--log-file=" + log_path)
            with self.lock:
                self.log_path = log_path
            run_cmd = full
            if cfg["keep_awake"] and shutil.which("caffeinate"):
                run_cmd = ["caffeinate", "-i"] + full
            self.add("$ " + mask(" ".join(shlex.quote(c) for c in full), key))
            try:
                proc = subprocess.Popen(run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1)
            except OSError as exc:
                self.add("Could not start immich-go: %s" % exc)
                break
            self.proc = proc
            stop_side = threading.Event()
            side = [threading.Thread(target=self._sample, args=(cfg, proc, stop_side), daemon=True)]
            if log_path:
                side.append(threading.Thread(target=self._follow_log, args=(log_path, key, stop_side), daemon=True))
            for t in side:
                t.start()
            said = []
            for line in proc.stdout:           # text mode also splits on the bare \r of status lines
                line = mask(line.rstrip("\r\n"), key)
                if line.strip():
                    said.append(line)
                    del said[:-30]
                    self._take(line, from_log=False)
            code = proc.wait()
            proc.stdout.close()
            stop_side.set()
            for t in side:
                t.join(timeout=5)
            refused = want_log and code != 0 and any("unknown flag" in s and "log-file" in s for s in said)
            if attempt == 1 and refused and not self.stopped:
                self.add("This immich-go does not accept --log-file; running again without it.")
                want_log = False
                continue
            break
        with self.lock:
            last = dict(self.progress) if self.progress else None
        if last:
            self.add("Immich read %(immich_read)d%%, Assets found: %(found)d, Upload errors: %(errors)d, "
                     "Uploaded %(uploaded)d" % last)
        note = ""
        if code == 0 and not self.dry_run and not self.stopped and cfg["eject_after"]:
            note = eject_for(self.source)
        with self.lock:
            self.running, self.returncode, self.finished, self.note = False, code, time.time(), note
        if note:
            self.add(note)

    def _count_source(self, source):
        totals = scan_folder(source, want_samples=True)
        samples = totals.pop("samples", [])
        with self.lock:
            if self.source == source:
                self.totals, self.samples = totals, samples

    def _follow_log(self, path, key, stop):
        """Read immich-go's log file as it grows."""
        fh = None
        rest = ""
        try:
            while True:
                done = stop.is_set()
                if fh is None:
                    try:
                        fh = open(path, "r", errors="replace")
                    except OSError:
                        if done:
                            return
                        time.sleep(0.3)
                        continue
                chunk = fh.read()
                if chunk:
                    rest += chunk
                    *whole, rest = rest.split("\n")
                    for line in whole:
                        if line.strip():
                            self._take(mask(line.rstrip("\r"), key), from_log=True)
                elif done:
                    if rest.strip():
                        self._take(mask(rest, key), from_log=True)
                    return
                else:
                    time.sleep(0.3)
        finally:
            if fh:
                fh.close()
            try:
                os.remove(path)
            except OSError:
                pass

    def _take(self, line, from_log):
        """Read one line of immich-go output. Status lines update the numbers
        instead of filling the log; event lines are counted; everything is kept
        for the log view (once, when the same event shows up in both places)."""
        m = PROGRESS_RE.search(line)
        if m:
            read, found, errors, uploaded = (int(x) for x in m.groups())
            now = time.time()
            with self.lock:
                self.progress = {"immich_read": read, "found": found, "errors": errors, "uploaded": uploaded}
                if not self.history or self.history[-1][1] != uploaded or now - self.history[-1][0] > 5:
                    self.history.append((now, uploaded))
                    del self.history[:-240]
            return
        ev = EVENT_RE.search(line)
        if ev:
            with self.lock:
                if from_log:
                    self.log_events = True
                elif self.log_events:
                    return                    # already counted from the log file
            self._event(EVENTS[ev.group(1)], ev.group(1), line)
        self.add(line)

    def _event(self, kind, text, line):
        f = FILE_ATTR_RE.search(line)
        name = ((f.group(1) or f.group(2)) if f else "").split(":", 1)[-1]
        size = 0
        if kind == "uploaded" and name:
            try:
                size = os.path.getsize(os.path.join(self.source, name))
            except OSError:
                size = 0
        with self.lock:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            if kind == "uploaded":
                self.bytes_up += size
                self.current = name
            if kind in FEED_KINDS and name:
                self.feed.append({"kind": kind, "what": text, "file": name, "size": size})
                del self.feed[:-8]

    def _sample(self, cfg, proc, stop):
        """Once a second, note how far we are and what the machine is doing."""
        host = urllib.parse.urlparse(cfg["server"]).hostname
        iface = route_interface(host)
        first = server_stats(cfg)
        with self.lock:
            self.interface, self.server, self.server_start = iface, first, dict(first)
        tick = 0
        while not stop.wait(1.0):
            tick += 1
            net = net_counters(iface)
            stats = process_stats(proc.pid) if tick % 2 == 0 else None
            fresh = server_stats(cfg) if tick % 5 == 0 else None
            with self.lock:
                up = self.counters.get("uploaded", 0) or (self.progress or {}).get("uploaded", 0)
                self.points.append({"t": round(time.time(), 1), "files": up, "bytes": self.bytes_up,
                                    "tx": net[0] if net else None})
                del self.points[:-self.MAX_POINTS]
                if stats:
                    self.proc_stats = stats
                if fresh:
                    self.server = fresh
        fresh = server_stats(cfg)
        with self.lock:
            if fresh:
                self.server = fresh

    def _rate(self):
        """Files per second over roughly the last minute (call with the lock held)."""
        if len(self.history) < 2:
            return None
        t1, u1 = self.history[-1]
        t0, u0 = next(((t, u) for t, u in self.history if t1 - t <= 60), self.history[0])
        if t1 - t0 < 3 or u1 <= u0:
            return None
        return (u1 - u0) / (t1 - t0)

    def stop(self):
        with self.lock:
            proc, running = self.proc, self.running
            self.stopped = True
        if proc and running:
            proc.terminate()
            return True
        return False

    def snapshot(self, since):
        with self.lock:
            start = max(since - self.offset, 0)
            return {"running": self.running, "returncode": self.returncode, "source": self.source,
                    "dry_run": self.dry_run, "started": self.started, "finished": self.finished,
                    "stopped": self.stopped, "next": self.offset + len(self.lines),
                    "lines": self.lines[start:], "progress": self.progress, "totals": self.totals,
                    "rate": self._rate() if self.running else None, "current": self.current,
                    "samples": list(self.samples), "counters": dict(self.counters),
                    "feed": list(self.feed), "bytes_up": self.bytes_up, "points": list(self.points),
                    "process": self.proc_stats, "server": dict(self.server),
                    "server_start": dict(self.server_start), "interface": self.interface}


def volume_of(path):
    """The card volume a path lives on, or None if it is not under the volumes root."""
    try:
        rel = Path(path).resolve().relative_to(VOLUMES_ROOT.resolve())
    except (ValueError, OSError):
        return None
    return str(VOLUMES_ROOT / rel.parts[0]) if rel.parts else None


def eject_for(path):
    volume = volume_of(path)
    if not volume:
        return "Not ejected: the source is not on a removable volume."
    if not shutil.which("diskutil"):
        return "Not ejected: diskutil is not available on this system."
    res = subprocess.run(["diskutil", "eject", volume], capture_output=True, text=True)
    if res.returncode == 0:
        return "Ejected %s. The card can be removed." % os.path.basename(volume)
    return "Could not eject %s: %s" % (os.path.basename(volume), (res.stderr or res.stdout).strip())


# ----------------------------------------------------------------- server ---

JOB = Job()
TOKEN = secrets.token_urlsafe(24)
STATE = {"config": None, "imported_from": None, "port": 0}


class Handler(BaseHTTPRequestHandler):
    server_version = APP_ID

    def log_message(self, *args):  # keep the terminal quiet
        pass

    # -- helpers
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        if not self._host_ok() or self.headers.get("X-Token") != TOKEN:
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except ValueError:
            return {}

    # -- routes
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        if url.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if url.path == "/api/hello":   # lets a second launch find the running copy
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            return self._send(200, {"app": APP_ID})
        if url.path == "/api/thumb":
            if not self._host_ok() or query.get("t", [""])[0] != TOKEN:
                return self._send(403, {"error": "forbidden"})
            thumb = make_thumb(query.get("path", [""])[0])
            if not thumb:
                return self._send(404, {"error": "no thumbnail"})
            ext = os.path.splitext(thumb)[1].lower()
            ctype = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/jpeg")
            try:
                with open(thumb, "rb") as fh:
                    data = fh.read()
            except OSError:
                return self._send(404, {"error": "no thumbnail"})
            return self._send(200, data, ctype)
        if not self._auth():
            return None
        cfg = STATE["config"]
        if url.path == "/api/state":
            return self._send(200, {
                "config": cfg, "cards": list_cards(), "drives": list_drives(), "immich_go": find_immich_go(cfg),
                "config_file": str(CONFIG_FILE), "imported_from": STATE["imported_from"],
                "options": ENUMS, "job": JOB.snapshot(10 ** 12)})
        if url.path == "/api/cards":
            return self._send(200, {"cards": list_cards(), "drives": list_drives()})
        if url.path == "/api/scan":
            path = query.get("path", [""])[0]
            if not os.path.isdir(path):
                return self._send(200, {"error": "Folder not found."})
            return self._send(200, scan_folder(path))
        if url.path == "/api/browse":
            return self._send(200, browse(query.get("path", [""])[0]))
        if url.path == "/api/log":
            since = int(query.get("since", ["0"])[0] or 0)
            return self._send(200, JOB.snapshot(since))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return None
        body = self._body()
        cfg = STATE["config"]
        if self.path == "/api/config":
            recent = cfg.get("recent_folders", [])
            cfg = clean_config(body)
            cfg["recent_folders"] = recent
            STATE["config"] = cfg
            try:
                save_config(cfg)
            except OSError as exc:
                return self._send(200, {"ok": False, "message": "Could not save: %s" % exc})
            return self._send(200, {"ok": True, "config": cfg, "immich_go": find_immich_go(cfg)})
        if self.path == "/api/test":
            return self._send(200, test_connection(cfg))
        if self.path == "/api/preview":
            source = str(body.get("source") or "")
            try:
                cmd = build_command(cfg, source or "<source folder>", bool(body.get("dry_run")),
                                    find_immich_go(cfg) or "immich-go")
            except ValueError as exc:
                return self._send(200, {"command": "Extra arguments could not be read: %s" % exc})
            return self._send(200, {"command": mask(" ".join(shlex.quote(c) for c in cmd), cfg["api_key"])})
        if self.path == "/api/run":
            source = str(body.get("source") or "")
            error = JOB.start(cfg, source, bool(body.get("dry_run")))
            if error:
                return self._send(200, {"ok": False, "message": error})
            if not any(c["path"] == source for c in list_cards()):
                recent = [source] + [r for r in cfg.get("recent_folders", []) if r != source]
                cfg["recent_folders"], cfg["last_folder"] = recent[:MAX_RECENT], source
                try:
                    save_config(cfg)
                except OSError:
                    pass
            return self._send(200, {"ok": True})
        if self.path == "/api/stop":
            return self._send(200, {"ok": JOB.stop()})
        if self.path == "/api/eject":
            if JOB.snapshot(10 ** 12)["running"]:
                return self._send(200, {"ok": False, "message": "Wait for the import to finish first."})
            message = eject_for(str(body.get("path") or ""))
            return self._send(200, {"ok": message.startswith("Ejected"), "message": message})
        return self._send(404, {"error": "not found"})


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>my_immich</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2044%2044%22%3E%3Cpath%20fill%3D%22%23e59a1f%22%20d%3D%22M12%205.5A3.5%203.5%200%200%201%2015.5%202H27l7%207v29.5a3.5%203.5%200%200%201-3.5%203.5h-15a3.5%203.5%200%200%201-3.5-3.5Z%22/%3E%3Cpath%20fill%3D%22%23fff%22%20opacity%3D%22.65%22%20d%3D%22M17%2027h12v9H17z%22/%3E%3Cpath%20fill%3D%22%23231703%22%20opacity%3D%22.55%22%20d%3D%22M17%207h2.600v7H17zm5%200h2.600v7H22z%22/%3E%3C/svg%3E">
<style>
  :root {
    color-scheme: light dark;
    --canvas:#e9ebee; --side:#dfe2e6; --panel:#ffffff; --raised:#f5f6f8;
    --ink:#1b1d22; --soft:#5d6470; --line:#d3d7de; --line-strong:#b9bfc9;
    --amber:#e59a1f; --amber-deep:#a8690a; --amber-wash:#fdf3e0; --on-amber:#231703;
    --good:#1c7c54; --bad:#b93a2e; --warn:#96600a;
    --term:#14161a; --term-ink:#d5d9e0; --term-dim:#8a919d;
    --r-s:7px; --r-m:11px; --r-l:16px;
    --ui: ui-rounded, "SF Pro Rounded", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    --text: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --canvas:#17181b; --side:#1c1e22; --panel:#22252a; --raised:#2a2d33;
      --ink:#eceef2; --soft:#9aa1ad; --line:#33373e; --line-strong:#474c55;
      --amber:#f0aa3c; --amber-deep:#f3bb62; --amber-wash:#33290f; --on-amber:#231703;
      --good:#58c794; --bad:#ff8a7d; --warn:#ecb354;
      --term:#101114; --term-ink:#d5d9e0; --term-dim:#7d8490;
    }
  }
  * { box-sizing:border-box; }
  [hidden] { display:none !important; }
  html, body { height:100%; }
  body { margin:0; background:var(--canvas); color:var(--ink); font:14.5px/1.45 var(--text);
    -webkit-font-smoothing:antialiased; }
  button, input, select { font:inherit; color:inherit; }
  :focus-visible { outline:2px solid var(--amber); outline-offset:2px; }

  /* ---- shell */
  .app { display:grid; grid-template-columns:216px minmax(0,1fr); height:100vh; }
  .side { background:var(--side); border-right:1px solid var(--line); display:flex; flex-direction:column;
    padding:18px 12px 14px; }
  .brand { display:flex; align-items:center; gap:10px; padding:2px 8px 18px; }
  .brand .mark { width:26px; height:32px; background:var(--amber); border-radius:4px;
    clip-path:polygon(0 0, 68% 0, 100% 24%, 100% 100%, 0 100%); position:relative; flex:none; }
  .brand .mark::after { content:""; position:absolute; left:5px; right:9px; top:5px; height:7px;
    background:repeating-linear-gradient(90deg, var(--on-amber) 0 2px, transparent 2px 5px); opacity:.55; }
  .brand b { font:600 17px/1 var(--ui); letter-spacing:-.01em; }
  .nav { display:grid; gap:2px; }
  .nav button { all:unset; display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:var(--r-s);
    cursor:pointer; color:var(--soft); font-weight:500; }
  .nav button:hover { background:color-mix(in srgb, var(--ink) 6%, transparent); color:var(--ink); }
  .nav button[aria-current="page"] { background:var(--panel); color:var(--ink);
    box-shadow:0 0 0 1px var(--line); }
  .nav button:focus-visible { outline:2px solid var(--amber); outline-offset:2px; }
  .nav svg { width:17px; height:17px; flex:none; }
  .nav .pip { margin-left:auto; width:8px; height:8px; border-radius:50%; background:var(--amber); }
  .health { margin-top:auto; display:grid; gap:8px; padding:12px 10px 0; border-top:1px solid var(--line);
    font-size:12.5px; color:var(--soft); }
  .health div { display:grid; grid-template-columns:10px minmax(0,1fr); gap:8px; align-items:start; }
  .dot { width:8px; height:8px; border-radius:50%; margin-top:5px; background:var(--line-strong); }
  .dot.good { background:var(--good); } .dot.bad { background:var(--bad); } .dot.busy { background:var(--amber); }
  .health span { overflow-wrap:anywhere; }

  main { min-width:0; height:100vh; display:flex; flex-direction:column; }
  .view { display:none; flex:1; min-height:0; flex-direction:column; }
  .view.on { display:flex; }
  .head { padding:20px 28px 14px; display:flex; align-items:flex-end; gap:16px; flex-wrap:wrap; }
  .head h1 { font:600 24px/1.1 var(--ui); margin:0; letter-spacing:-.015em; }
  .head p { margin:0; color:var(--soft); }
  .head .grow { flex:1; }

  /* ---- controls */
  .btn { border:1px solid var(--line-strong); background:var(--panel); border-radius:var(--r-s);
    padding:7px 14px; cursor:pointer; font-weight:500; white-space:nowrap; }
  .btn:hover:not(:disabled) { background:var(--raised); }
  .btn.go { background:var(--amber); border-color:var(--amber); color:var(--on-amber); font-weight:650;
    padding:10px 20px; border-radius:var(--r-m); font-size:15px; }
  .btn.go:hover:not(:disabled) { background:color-mix(in srgb, var(--amber) 88%, #000); }
  .btn.big { padding:10px 18px; border-radius:var(--r-m); font-size:15px; }
  .btn.stop { color:var(--bad); border-color:color-mix(in srgb, var(--bad) 55%, var(--line)); }
  .btn.quiet { border-color:transparent; background:none; color:var(--soft); padding:6px 10px; }
  .btn.quiet:hover:not(:disabled) { background:color-mix(in srgb, var(--ink) 7%, transparent); color:var(--ink); }
  .btn:disabled { opacity:.45; cursor:not-allowed; }
  input[type=text], input[type=password], select { width:100%; padding:8px 10px; border:1px solid var(--line-strong);
    border-radius:var(--r-s); background:var(--panel); }
  .msg { font-size:13.5px; color:var(--soft); }
  .msg.good { color:var(--good); } .msg.bad { color:var(--bad); } .msg.warn { color:var(--warn); }

  /* ---- import view */
  .work { flex:1; min-height:0; display:grid; grid-template-columns:minmax(280px, 380px) minmax(0,1fr);
    gap:18px; padding:0 28px 24px; }
  .col { min-height:0; display:flex; flex-direction:column; gap:10px; }
  .col h2 { font:600 13.5px/1 var(--text); color:var(--soft); margin:6px 2px 2px; }
  .sources { overflow:auto; display:grid; gap:10px; align-content:start; padding:2px; margin:-2px; }
  .tile { display:grid; grid-template-columns:44px minmax(0,1fr); gap:14px; align-items:center;
    background:var(--panel); border:1px solid var(--line); border-radius:var(--r-l); padding:14px 16px;
    cursor:pointer; position:relative; }
  .tile:hover { border-color:var(--line-strong); }
  .tile.sel { border-color:var(--amber); box-shadow:0 0 0 1px var(--amber); background:var(--amber-wash); }
  .tile input[type=radio] { position:absolute; opacity:0; pointer-events:none; }
  .tile:has(input:focus-visible) { outline:2px solid var(--amber); outline-offset:2px; }
  .glyph { width:38px; height:48px; border-radius:5px; background:var(--raised); border:1px solid var(--line-strong);
    position:relative; justify-self:center; }
  .glyph.card { background:var(--amber); border:0; clip-path:polygon(0 0, 68% 0, 100% 22%, 100% 100%, 0 100%); }
  .glyph.card::before { content:""; position:absolute; left:7px; right:12px; top:7px; height:10px;
    background:repeating-linear-gradient(90deg, var(--on-amber) 0 3px, transparent 3px 7px); opacity:.5; }
  .glyph.card::after { content:""; position:absolute; left:7px; right:7px; bottom:8px; height:13px; border-radius:2px;
    background:color-mix(in srgb, #fff 55%, transparent); }
  .glyph.dir { height:34px; margin-top:8px; border-radius:4px 6px 6px 6px; }
  .glyph.dir::before { content:""; position:absolute; left:-1px; top:-7px; width:17px; height:8px;
    border:1px solid var(--line-strong); border-bottom:0; border-radius:4px 4px 0 0; background:var(--raised); }
  .tile .name { font:600 16px/1.2 var(--ui); overflow-wrap:anywhere; }
  .tile .meta { color:var(--soft); font-size:13px; overflow-wrap:anywhere; margin-top:2px; }
  .tile .count { font-size:13.5px; margin-top:6px; font-variant-numeric:tabular-nums; }
  .chips { display:flex; gap:5px; flex-wrap:wrap; margin-top:7px; }
  .chip { font-size:11.5px; padding:1px 8px; border-radius:99px; border:1px solid var(--line-strong);
    color:var(--soft); background:var(--panel); }
  .empty { border:1px dashed var(--line-strong); border-radius:var(--r-l); padding:18px 16px; color:var(--soft); }
  .empty b { display:block; color:var(--ink); font:600 15px/1.3 var(--ui); margin-bottom:3px; }
  .quick { display:grid; gap:8px; }
  .quick .row { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .quick .row > span { color:var(--soft); font-size:12.5px; width:58px; flex:none; }
  .quick button { all:unset; cursor:pointer; font-size:12.5px; padding:3px 10px; border-radius:99px;
    border:1px solid var(--line-strong); background:var(--panel); max-width:100%; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; box-sizing:border-box; }
  .quick button:hover { background:var(--raised); }
  .quick button:focus-visible { outline:2px solid var(--amber); outline-offset:2px; }

  dialog { border:1px solid var(--line-strong); border-radius:var(--r-l); padding:0; background:var(--panel);
    color:var(--ink); width:min(860px, calc(100vw - 32px)); box-shadow:0 24px 70px rgba(0,0,0,.35); }
  dialog::backdrop { background:rgba(10,12,16,.45); }
  .chooser { display:flex; flex-direction:column; height:min(620px, calc(100vh - 64px)); }
  .chooser header { padding:16px 18px 10px; border-bottom:1px solid var(--line); display:grid; gap:10px; }
  .chooser header b { font:600 17px/1.2 var(--ui); }
  .pathrow { display:flex; gap:8px; }
  .pathrow input { font:12.5px var(--mono); }
  .crumbs { display:flex; flex-wrap:wrap; gap:2px; align-items:center; font-size:13px; }
  .crumbs button { all:unset; cursor:pointer; padding:2px 7px; border-radius:var(--r-s); color:var(--soft); }
  .crumbs button:hover, .crumbs button:focus-visible { background:var(--raised); color:var(--ink); }
  .crumbs button[aria-current] { color:var(--ink); font-weight:600; }
  .crumbs i { color:var(--line-strong); font-style:normal; }
  .panes { flex:1; min-height:0; display:grid; grid-template-columns:190px minmax(0,1fr); }
  .places { list-style:none; margin:0; padding:8px; border-right:1px solid var(--line); overflow:auto;
    background:var(--raised); }
  .places li.sep { height:1px; background:var(--line); margin:6px 4px; }
  .places button, .listing li button { all:unset; display:flex; align-items:center; gap:9px; width:100%;
    box-sizing:border-box; padding:6px 9px; border-radius:var(--r-s); cursor:pointer; overflow:hidden; }
  .places button span, .listing li button span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .places button:hover, .places button:focus-visible { background:color-mix(in srgb, var(--ink) 8%, transparent); }
  .places button[aria-current] { background:var(--amber-wash); box-shadow:0 0 0 1px var(--amber) inset; }
  .ico { width:15px; height:12px; border-radius:2px 3px 3px 3px; background:var(--line-strong); flex:none; position:relative; }
  .ico::before { content:""; position:absolute; left:0; top:-3px; width:7px; height:3px; border-radius:2px 2px 0 0;
    background:var(--line-strong); }
  .ico.card { width:12px; height:15px; border-radius:2px; background:var(--amber);
    clip-path:polygon(0 0, 66% 0, 100% 24%, 100% 100%, 0 100%); }
  .ico.card::before { display:none; }
  .ico.drive { height:11px; border-radius:3px; background:none; border:1.5px solid var(--soft); }
  .ico.drive::before { left:auto; right:2px; top:2.5px; width:3px; height:3px; border-radius:50%; background:var(--soft); }
  .listing { overflow:auto; padding:8px; }
  .listing ul { list-style:none; margin:0; padding:0; }
  .listing li button:hover, .listing li button:focus-visible { background:var(--raised); }
  .listing .msg { margin:6px 9px; }
  .chooser footer { border-top:1px solid var(--line); padding:12px 18px; display:flex; gap:10px; align-items:center;
    flex-wrap:wrap; }
  .chooser footer .grow { flex:1; }
  .chooser footer .btn.go { padding:8px 16px; font-size:inherit; border-radius:var(--r-s); }
  @media (max-width: 640px) { .panes { grid-template-columns:1fr; } .places { display:flex; flex-wrap:wrap; gap:2px;
    border-right:0; border-bottom:1px solid var(--line); } .places button { width:auto; } .places li.sep { display:none; } }

  .run { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-l); padding:16px;
    display:flex; flex-direction:column; gap:12px; min-height:0; flex:1; overflow-y:auto; }
  .run > * { flex-shrink:0; }
  .actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .actions .grow { flex:1; }
  .run > .msg { margin:0; min-height:21px; }
  .prog { display:grid; gap:12px; }
  .phase { display:flex; align-items:baseline; justify-content:space-between; gap:16px; }
  .phase span { font:600 16px/1.3 var(--ui); overflow-wrap:anywhere; }
  .phase b { font:600 30px/1 var(--ui); font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
  .bar { height:10px; border-radius:99px; background:var(--raised); border:1px solid var(--line); overflow:hidden; }
  .bar i { display:block; height:100%; width:0; background:var(--amber); border-radius:99px; transition:width .6s ease; }
  .bar.wait i { width:28%; animation:slide 1.5s ease-in-out infinite; }
  .bar.done i { background:var(--good); } .bar.fail i { background:var(--bad); }
  @keyframes slide { 0% { transform:translateX(-110%); } 100% { transform:translateX(370%); } }
  .stats { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:8px; margin:0; }
  .stats div { background:var(--raised); border-radius:var(--r-m); padding:9px 12px; min-width:0; }
  .stats dt { font-size:12px; color:var(--soft); }
  .stats dd { margin:1px 0 0; font:600 15.5px/1.3 var(--ui); font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  .stats dd small { display:block; font:400 12px/1.3 var(--text); color:var(--soft); }
  .stats dd.bad { color:var(--bad); }
  /* contact sheet: frames start out under the safelight and "develop" as the import passes them */
  .sheet { display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:5px; background:var(--term);
    border-radius:var(--r-m); padding:8px; }
  .sheet:empty { display:none; }
  .frame { position:relative; aspect-ratio:3 / 2; border-radius:3px; overflow:hidden; background:#23201a; }
  .frame img { width:100%; height:100%; object-fit:cover; display:block;
    filter:grayscale(1) sepia(1) saturate(2.4) hue-rotate(-12deg) brightness(.42); transition:filter 1.1s ease; }
  .frame.dev img { filter:none; }
  .frame span { position:absolute; inset:0; display:grid; place-items:center; font:11px var(--mono); color:#8f8468; }
  .frame.dev span { color:#e9e2d0; }
  @media (prefers-reduced-motion: reduce) { .bar i, .frame img { transition:none; } .bar.wait i { animation:none; width:100%; opacity:.5; } }
  .chart { margin:0; background:var(--raised); border-radius:var(--r-m); padding:10px 12px 0; overflow:hidden; }
  .chart figcaption { display:flex; align-items:baseline; gap:10px; font-size:12px; color:var(--soft); }
  .chart figcaption b { font:600 15.5px/1.2 var(--ui); color:var(--ink); font-variant-numeric:tabular-nums; }
  .chart figcaption small { margin-left:auto; font-size:12px; font-variant-numeric:tabular-nums; }
  .chart svg { display:block; width:calc(100% + 24px); margin:6px -12px 0; height:70px; }
  #chartArea { fill:var(--amber); opacity:.22; } #chartLine { fill:none; stroke:var(--amber); stroke-width:2;
    vector-effect:non-scaling-stroke; stroke-linejoin:round; stroke-linecap:round; }
  .geek { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:1px; margin:0; background:var(--line);
    border:1px solid var(--line); border-radius:var(--r-m); overflow:hidden; }
  .geek:empty, .ledger:empty, .feed:empty { display:none; }
  .geek div { background:var(--panel); padding:8px 12px; min-width:0; }
  .geek dt { font-size:11.5px; color:var(--soft); }
  .geek dd { margin:0; font:500 13px/1.35 var(--mono); font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  .geek dd small { display:block; font:400 11.5px/1.3 var(--text); color:var(--soft); }
  .ledger { display:flex; flex-wrap:wrap; gap:6px; }
  .ledger span { font-size:12.5px; padding:2px 9px; border-radius:99px; border:1px solid var(--line-strong);
    font-variant-numeric:tabular-nums; }
  .ledger span b { font-weight:650; margin-right:4px; }
  .ledger span.bad { border-color:var(--bad); color:var(--bad); }
  .ledger span.go { border-color:var(--amber); background:var(--amber-wash); }
  .feed { list-style:none; margin:0; padding:0; display:grid; gap:1px; font:12px/1.5 var(--mono); }
  .feed li { display:grid; grid-template-columns:8px minmax(0,1fr) auto; gap:9px; align-items:center; padding:2px 2px; }
  .feed li i { width:7px; height:7px; border-radius:50%; background:var(--amber); }
  .feed li.skip i { background:var(--line-strong); } .feed li.bad i { background:var(--bad); }
  .feed li span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .feed li small { color:var(--soft); font:11.5px var(--text); white-space:nowrap; }
  .feed li:first-child { animation:arrive .35s ease; }
  @keyframes arrive { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:none; } }
  @media (prefers-reduced-motion: reduce) { .feed li:first-child { animation:none; } }
  .rest { flex:1; min-height:140px; display:grid; place-items:center; align-content:center; gap:14px; text-align:center;
    color:var(--soft); border:1px dashed var(--line-strong); border-radius:var(--r-m); padding:20px; }
  .rest p { margin:0; max-width:38ch; } .rest .glyph { opacity:.55; }
  details.cmd summary { cursor:pointer; color:var(--soft); font-size:13px; width:max-content; }
  details.cmd pre { margin:8px 0 0; padding:10px 12px; background:var(--raised); border-radius:var(--r-s);
    font:12px/1.5 var(--mono); white-space:pre-wrap; overflow-wrap:anywhere; color:var(--soft); }
  #log { height:300px; resize:vertical; margin:8px 0 0; overflow:auto; background:var(--term); color:var(--term-ink);
    border-radius:var(--r-m); padding:12px 14px; font:12px/1.55 var(--mono); white-space:pre-wrap;
    overflow-wrap:anywhere; }
  #log.idle { color:var(--term-dim); display:grid; place-items:center; text-align:center; font-family:var(--text);
    font-size:13.5px; white-space:normal; }

  /* ---- setup view */
  .guide { flex:1; min-height:0; overflow:auto; padding:0 28px 28px; }
  .steps { list-style:none; margin:0; padding:0; max-width:760px; display:grid; gap:12px; counter-reset:step; }
  .step { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-l); padding:16px 18px 16px 62px;
    position:relative; counter-increment:step; }
  .step::before { content:counter(step); position:absolute; left:18px; top:15px; width:28px; height:28px;
    border-radius:50%; display:grid; place-items:center; font:600 14px/1 var(--ui);
    border:1.5px solid var(--line-strong); color:var(--soft); }
  .step.done::before { content:""; background:var(--good); border-color:var(--good);
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'%3E%3Cpath d='M8 14.500l4 4 8-9' fill='none' stroke='%23fff' stroke-width='2.600' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"); }
  .step h2 { font:600 16px/1.3 var(--ui); margin:0 0 3px; }
  .step p { margin:0 0 8px; color:var(--soft); max-width:64ch; }
  .step p:last-child { margin-bottom:0; }
  .step .now { font-size:13.5px; margin:8px 0 0; }
  .step .now.good { color:var(--good); } .step .now.bad { color:var(--bad); }
  .step .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:8px 0 0; }
  .code { display:flex; align-items:center; gap:8px; background:var(--raised); border:1px solid var(--line);
    border-radius:var(--r-s); padding:6px 6px 6px 12px; font:12.5px/1.5 var(--mono); max-width:100%; }
  .code code { overflow-wrap:anywhere; flex:1; min-width:0; }
  .code .btn { padding:3px 10px; font:500 12.5px var(--text); flex:none; }
  .step details summary { cursor:pointer; color:var(--soft); font-size:13.5px; width:max-content; margin-top:6px; }
  .step a.btn { color:inherit; text-decoration:none; display:inline-block; }
  .tips { max-width:760px; margin:22px 0 0; }
  .tips h2 { font:600 15px/1.2 var(--ui); margin:0 0 8px 2px; }
  .tips dl { margin:0; background:var(--panel); border:1px solid var(--line); border-radius:var(--r-l); }
  .tips dt { font-weight:600; padding:12px 18px 0; }
  .tips dd { margin:0; padding:2px 18px 12px; color:var(--soft); border-bottom:1px solid var(--line); max-width:none; }
  .tips dd:last-child { border-bottom:0; }

  /* ---- settings view */
  .form { flex:1; min-height:0; overflow:auto; padding:0 28px 8px; }
  .group { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-l); max-width:760px;
    margin:0 0 16px; }
  .group h2 { font:600 15px/1.2 var(--ui); margin:0; padding:14px 18px 4px; }
  .group .note { margin:0; padding:0 18px 8px; color:var(--soft); font-size:13px; }
  .field { display:grid; grid-template-columns:minmax(150px, 230px) minmax(0,1fr); gap:6px 18px; align-items:center;
    padding:10px 18px; border-top:1px solid var(--line); }
  .field > label, .field > .lab { font-weight:500; }
  .field small { display:block; color:var(--soft); font-weight:400; font-size:12.5px; margin-top:1px; }
  .field .inline { display:flex; gap:8px; }
  .switch { justify-self:end; position:relative; width:40px; height:24px; flex:none; }
  .switch input { position:absolute; inset:0; opacity:0; margin:0; cursor:pointer; width:100%; height:100%; }
  .switch i { position:absolute; inset:0; border-radius:99px; background:var(--line-strong); pointer-events:none;
    transition:background .15s; }
  .switch i::after { content:""; position:absolute; left:2px; top:2px; width:20px; height:20px; border-radius:50%;
    background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.25); transition:transform .15s; }
  .switch input:checked + i { background:var(--amber); }
  .switch input:checked + i::after { transform:translateX(16px); }
  .switch input:focus-visible + i { outline:2px solid var(--amber); outline-offset:2px; }
  @media (prefers-reduced-motion: reduce) { .switch i, .switch i::after { transition:none; } }
  .savebar { border-top:1px solid var(--line); background:var(--canvas); padding:12px 28px; display:flex; gap:10px;
    align-items:center; flex-wrap:wrap; }
  .savebar .file { margin-left:auto; font-size:12px; color:var(--soft); overflow-wrap:anywhere; max-width:46ch;
    text-align:right; }

  @media (max-width: 860px) {
    .app { grid-template-columns:1fr; grid-template-rows:auto minmax(0,1fr); height:auto; min-height:100vh; }
    .side { flex-direction:row; align-items:center; gap:10px; padding:10px 12px; border-right:0;
      border-bottom:1px solid var(--line); flex-wrap:wrap; }
    .brand { padding:0 6px 0 2px; } .nav { display:flex; } .health { display:none; }
    main { height:auto; } .work { grid-template-columns:1fr; padding:0 16px 20px; }
    .head { padding:16px 16px 10px; } .form { padding:0 16px 8px; } .guide { padding:0 16px 24px; } .savebar { padding:12px 16px; }
    .field { grid-template-columns:1fr; } .switch { justify-self:start; }
    .stats, .geek { grid-template-columns:repeat(2, minmax(0,1fr)); } .sheet { grid-template-columns:repeat(4, minmax(0,1fr)); }
    #log { min-height:260px; } .savebar .file { margin-left:0; text-align:left; }
  }
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand"><span class="mark" aria-hidden="true"></span><b>my_immich</b></div>
    <nav class="nav" aria-label="Sections">
      <button type="button" data-view="import" aria-current="page">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13V3M6 7l4-4 4 4M3 13v2.5A1.5 1.5 0 0 0 4.5 17h11a1.5 1.5 0 0 0 1.5-1.5V13"/></svg>
        Import</button>
      <button type="button" data-view="settings">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M3 6h8M15 6h2M3 14h2M9 14h8"/><circle cx="13" cy="6" r="2"/><circle cx="7" cy="14" r="2"/></svg>
        Settings<span class="pip" id="dirtyPip" hidden title="Unsaved changes"></span></button>
      <button type="button" data-view="setup">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 3.5h10a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1Z"/><path d="M7 8l1.300 1.300L10.500 7M7 12.500l1.300 1.300 2.200-2.300M12.500 8.500h1M12.500 13h1"/></svg>
        Setup<span class="pip" id="setupPip" hidden title="Setup is not finished"></span></button>
    </nav>
    <div class="health">
      <div><i class="dot" id="srvDot"></i><span id="srvText">Checking Immich…</span></div>
      <div><i class="dot" id="goDot"></i><span id="goText">Looking for immich-go…</span></div>
    </div>
  </aside>

  <main>
    <!-- Import -->
    <section class="view on" id="view-import" aria-labelledby="h-import">
      <div class="head">
        <div><h1 id="h-import">Import</h1><p id="sourceLine">Choose what to import.</p></div>
      </div>
      <div class="work">
        <div class="col">
          <h2>From</h2>
          <div class="sources">
            <div id="cards"></div>
            <label class="tile" id="folderSrc">
              <input type="radio" name="src" value="__folder__">
              <span class="glyph dir" aria-hidden="true"></span>
              <span>
                <span class="name">A folder</span>
                <span class="meta" id="folderPath" style="display:block">Any folder, USB drive or external disk</span>
                <span class="count" id="folderCount" style="display:block" hidden></span>
                <span class="chips"><button type="button" class="btn" id="browseBtn">Choose folder</button></span>
              </span>
            </label>
          </div>
          <div class="quick" id="quick"></div>
          <div class="actions">
            <button type="button" class="btn quiet" id="rescan">Look for cards and drives again</button>
            <span class="msg" id="scanInfo"></span>
          </div>
        </div>

        <div class="col">
          <h2>To Immich</h2>
          <div class="run">
            <div class="actions">
              <button type="button" class="btn go" id="run">Import to Immich</button>
              <button type="button" class="btn big" id="dry">Preview first</button>
              <button type="button" class="btn big stop" id="stop" hidden>Stop</button>
              <span class="grow"></span>
              <button type="button" class="btn quiet" id="eject" hidden>Eject card</button>
            </div>
            <p class="msg" id="runInfo">A preview shows what would be uploaded without sending anything.</p>
            <div class="prog" id="prog" hidden>
              <div class="phase"><span id="pPhase">Starting</span><b id="pPct">0%</b></div>
              <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-labelledby="pPhase" id="pBarBox"><i id="pBar"></i></div>
              <dl class="stats">
                <div><dt>Done</dt><dd id="pDone">0</dd></div>
                <div><dt>Time left</dt><dd id="pLeft">Working it out</dd></div>
                <div><dt>Pace</dt><dd id="pRate">Working it out</dd></div>
                <div><dt>Elapsed</dt><dd id="pTime">0s</dd></div>
              </dl>
              <figure class="chart" id="chartBox" hidden>
                <figcaption><span id="chartName">Upload speed</span><b id="chartNow"></b><small id="chartPeak"></small></figcaption>
                <svg id="chart" viewBox="0 0 600 70" preserveAspectRatio="none" aria-hidden="true">
                  <path id="chartArea" d=""></path><path id="chartLine" d=""></path></svg>
              </figure>
              <div class="sheet" id="sheet" aria-hidden="true"></div>
              <dl class="geek" id="geek"></dl>
              <div class="ledger" id="ledger"></div>
              <ol class="feed" id="feed" aria-label="Latest files"></ol>
              <p class="msg" id="pNote"></p>
            </div>
            <details class="cmd"><summary>Show the immich-go command</summary><pre id="cmd"></pre></details>
            <details class="cmd logbox" id="logBox"><summary>Show the full log</summary>
              <pre id="log" class="idle" aria-live="off">The import log appears here.</pre></details>
            <div class="rest" id="rest">
              <span class="glyph card" aria-hidden="true"></span>
              <p>Progress, a contact sheet of the photos and the result appear here once an import or preview starts.</p>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- Setup -->
    <section class="view" id="view-setup" aria-labelledby="h-setup">
      <div class="head"><div><h1 id="h-setup">Setup</h1><p id="setupLine">Five steps, done once.</p></div></div>
      <div class="guide">
        <ol class="steps">
          <li class="step" id="st-go">
            <h2>Install immich-go</h2>
            <p>my_immich uses immich-go to do the uploading. Install it with Homebrew in Terminal. Immich v3 needs immich-go 0.32 or later.</p>
            <div class="code"><code>brew install immich-go</code><button type="button" class="btn" data-copy="brew install immich-go">Copy</button></div>
            <p class="now" id="st-go-now"></p>
          </li>
          <li class="step" id="st-server">
            <h2>Enter your Immich server address</h2>
            <p>The address you open Immich with in a browser, including the port, for example http://192.168.1.10:2283.</p>
            <div class="row"><button type="button" class="btn" data-go="settings" data-focus="server">Open Settings</button></div>
            <p class="now" id="st-server-now"></p>
          </li>
          <li class="step" id="st-key">
            <h2>Create an API key and paste it in</h2>
            <p>In Immich, open Account Settings, then API Keys, and choose New API Key. Give it all permissions, or the ones listed here. Copy the key and paste it into Settings.</p>
            <div class="row">
              <a class="btn" id="keyLink" target="_blank" rel="noopener" hidden>Open API Keys in Immich</a>
              <button type="button" class="btn" data-go="settings" data-focus="api_key">Open Settings</button>
            </div>
            <details><summary>Permissions the key needs</summary>
              <div class="code" style="margin-top:8px; align-items:flex-start"><code id="permList">asset.read, asset.statistics, asset.update, asset.upload, asset.copy, asset.delete, asset.download, album.create, album.read, albumAsset.create, server.about, stack.create, tag.create, tag.asset, user.read, job.create, job.read</code><button type="button" class="btn" data-copy-from="permList">Copy</button></div>
              <p style="margin-top:8px">job.create and job.read are only for pausing Immich's background jobs, and need an admin account's key. An import that stops with "Missing required permission" names the one to add.</p>
            </details>
            <p class="now" id="st-key-now"></p>
          </li>
          <li class="step" id="st-test">
            <h2>Test the connection</h2>
            <p>Checks that the server answers and accepts the key.</p>
            <div class="row"><button type="button" class="btn" id="setupTest">Test connection</button></div>
            <p class="now" id="st-test-now"></p>
          </li>
          <li class="step" id="st-run">
            <h2>Preview, then import</h2>
            <p>Insert a card or choose a folder, press Preview first to see what would be uploaded, then Import to Immich. Files already in Immich are skipped, so importing the same card twice is harmless.</p>
            <div class="row"><button type="button" class="btn" data-go="import">Go to Import</button></div>
            <p class="now" id="st-run-now"></p>
          </li>
        </ol>

        <div class="tips">
          <h2>Good to know</h2>
          <dl>
            <dt>Start at login</dt>
            <dd>System Settings, General, Login Items: add my_immich. It then waits in the menu bar and tells you when a card is inserted.</dd>
            <dt>Folders on external drives</dt>
            <dd>The first time you open an external drive, Desktop, Documents or Downloads, macOS asks whether Python may read it. Allow it, or the folder shows as unreadable.</dd>
            <dt>Date folders on the server</dt>
            <dd>To have Immich file originals by year, month and day, turn on Storage Template in Immich's Administration settings. my_immich does not change this.</dd>
            <dt>Before formatting a card</dt>
            <dd>Check the photos in Immich first. With "Keep RAW only", the JPEG twin of a RAW+JPEG pair is never uploaded.</dd>
            <dt>Quitting</dt>
            <dd>Quit from the menu-bar icon. Quitting during an import stops it; run the import again to carry on.</dd>
            <dt>Where settings are kept</dt>
            <dd id="tipCfg">In a file in your home folder that also holds the API key.</dd>
          </dl>
        </div>
      </div>
    </section>

    <!-- Settings -->
    <section class="view" id="view-settings" aria-labelledby="h-settings">
      <div class="head"><div><h1 id="h-settings">Settings</h1><p>Used for every import until you change them.</p></div></div>
      <div class="form" id="settingsForm">
        <div class="group">
          <h2>Immich server</h2>
          <div class="field"><label for="server">Address</label>
            <input type="text" id="server" placeholder="http://192.168.1.10:2283" spellcheck="false" autocomplete="off"></div>
          <div class="field"><label for="api_key">API key<small>From Immich, Account Settings, API Keys</small></label>
            <span class="inline"><input type="password" id="api_key" autocomplete="off" spellcheck="false">
              <button type="button" class="btn" id="showKey">Show</button></span></div>
          <div class="field"><span class="lab">Connection</span>
            <span class="inline" style="align-items:center"><button type="button" class="btn" id="test">Test connection</button>
              <span class="msg" id="testInfo"></span></span></div>
        </div>

        <div class="group">
          <h2>What gets uploaded</h2>
          <div class="field"><label for="raw_jpeg">RAW + JPEG pairs<small>When a shot exists as both</small></label><select id="raw_jpeg"></select></div>
          <div class="field"><label for="burst">Bursts</label><select id="burst"></select></div>
          <div class="field"><label for="heic_jpeg">HEIC + JPEG pairs</label><select id="heic_jpeg"></select></div>
          <div class="field"><label for="include_type">Kinds of files</label><select id="include_type"></select></div>
          <div class="field"><label for="recursive">Include subfolders<small>Needed for cards laid out as DCIM/100CANON</small></label>
            <span class="switch"><input type="checkbox" id="recursive"><i></i></span></div>
        </div>

        <div class="group">
          <h2>Organizing</h2>
          <div class="field"><label for="session_tag">Tag each import with its time<small>Makes a batch easy to find later</small></label>
            <span class="switch"><input type="checkbox" id="session_tag"><i></i></span></div>
          <div class="field"><label for="folder_as_album">Albums from folders</label><select id="folder_as_album"></select></div>
          <div class="field"><label for="into_album">Put everything in one album</label>
            <input type="text" id="into_album" placeholder="Leave empty for none"></div>
        </div>

        <div class="group">
          <h2>While importing</h2>
          <div class="field"><label for="on_errors">If a file fails</label><select id="on_errors"></select></div>
          <div class="field"><label for="pause_jobs">Pause Immich's background jobs<small>Needs an admin API key</small></label>
            <span class="switch"><input type="checkbox" id="pause_jobs"><i></i></span></div>
          <div class="field"><label for="keep_awake">Keep this Mac awake</label>
            <span class="switch"><input type="checkbox" id="keep_awake"><i></i></span></div>
          <div class="field"><label for="eject_after">Eject the card when done<small>Only after a real import succeeds</small></label>
            <span class="switch"><input type="checkbox" id="eject_after"><i></i></span></div>
        </div>

        <div class="group">
          <h2>Advanced</h2>
          <div class="field"><label for="immich_go_path">immich-go location<small>Leave empty to find it automatically</small></label>
            <input type="text" id="immich_go_path" spellcheck="false" placeholder="/opt/homebrew/bin/immich-go"></div>
          <div class="field"><label for="extra_args">Extra arguments<small>Passed to immich-go as written</small></label>
            <input type="text" id="extra_args" spellcheck="false" placeholder="--date-range=2026"></div>
        </div>
      </div>
      <div class="savebar">
        <button type="button" class="btn go" style="padding:8px 18px" id="save">Save settings</button>
        <button type="button" class="btn" id="revert" hidden>Discard changes</button>
        <span class="msg" id="saveInfo"></span>
        <span class="file" id="cfgPath"></span>
      </div>
    </section>
  </main>
</div>

<dialog id="picker" aria-labelledby="pickTitle">
  <form method="dialog" class="chooser" id="pickForm">
    <header>
      <b id="pickTitle">Choose a folder to import</b>
      <div class="pathrow">
        <input type="text" id="pickInput" spellcheck="false" autocomplete="off" aria-label="Folder path">
        <button type="button" class="btn" id="pickGo">Go</button>
      </div>
      <nav class="crumbs" id="pickCrumbs" aria-label="Path"></nav>
    </header>
    <div class="panes">
      <ul class="places" id="pickPlaces" aria-label="Places"></ul>
      <div class="listing">
        <p class="msg bad" id="pickError" hidden></p>
        <ul id="pickList" aria-label="Folders"></ul>
      </div>
    </div>
    <footer>
      <span class="msg" id="pickInfo"></span>
      <span class="grow"></span>
      <button type="button" class="btn" id="pickCancel">Cancel</button>
      <button type="button" class="btn go" id="pickUse">Use this folder</button>
    </footer>
  </form>
</dialog>

<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
const FIELDS = ["server","api_key","raw_jpeg","burst","heic_jpeg","on_errors","folder_as_album","into_album",
  "include_type","immich_go_path","extra_args"];
const CHECKS = ["session_tag","pause_jobs","keep_awake","eject_after","recursive"];
const LABELS = {
  raw_jpeg:{NoStack:"Upload both, unstacked",KeepRaw:"Keep RAW only",KeepJPG:"Keep JPEG only",
    StackCoverRaw:"Stack both, RAW on top",StackCoverJPG:"Stack both, JPEG on top"},
  burst:{NoStack:"Leave as separate photos",Stack:"Stack bursts",StackKeepRaw:"Stack, keep RAW only",StackKeepJPEG:"Stack, keep JPEG only"},
  heic_jpeg:{NoStack:"Upload both, unstacked",KeepHeic:"Keep HEIC only",KeepJPG:"Keep JPEG only",
    StackCoverHeic:"Stack both, HEIC on top",StackCoverJPG:"Stack both, JPEG on top"},
  folder_as_album:{NONE:"No albums",FOLDER:"One album per folder name",PATH:"One album per full path"},
  include_type:{all:"Photos and videos",IMAGE:"Photos only",VIDEO:"Videos only"},
  on_errors:{stop:"Stop the import",continue:"Skip it and continue"}
};
let state = null, selected = null, folder = "", logNext = 0, polling = null,
    pickAt = "", dirty = false, running = false, conn = null, ranOnce = false;
const scans = {};

async function api(path, body) {
  const opt = {headers:{"X-Token":TOKEN}};
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  if (!res.ok) throw new Error("Request failed (" + res.status + ")");
  return res.json();
}
function fmtBytes(n) {
  const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
function fmtTime(s) {
  s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? h + "h " + (m % 60) + "m" : m ? m + "m " + (s % 60) + "s" : s + "s";
}
function say(el, text, cls) { el.textContent = text; el.className = "msg" + (cls ? " " + cls : ""); }
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

/* ---- navigation */
function show(view) {
  for (const b of document.querySelectorAll(".nav button")) {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  for (const v of document.querySelectorAll(".view")) v.classList.toggle("on", v.id === "view-" + view);
  if (location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
}
document.querySelectorAll(".nav button").forEach(b => b.onclick = () => show(b.dataset.view));

/* ---- setup guide */
function mark(id, done, text, cls) {
  $(id).classList.toggle("done", !!done);
  const now = $(id + "-now"); now.textContent = text || ""; now.className = "now" + (cls ? " " + cls : "");
}
function renderSetup() {
  if (!state) return;
  const c = state.config, go = !!state.immich_go, srv = !!c.server, key = !!c.api_key, ok = !!(conn && conn.ok);
  mark("st-go", go, go ? "Installed at " + state.immich_go : "Not found yet. After installing, reload this page.", go ? "good" : "bad");
  mark("st-server", srv, srv ? "Set to " + c.server : "Not set yet.", srv ? "good" : "");
  mark("st-key", key, key ? "A key is saved." : "No key saved yet.", key ? "good" : "");
  mark("st-test", ok, conn ? conn.message : (srv && key ? "Not tested yet." : "Finish the steps before this one first."), conn ? (ok ? "good" : "bad") : "");
  const ran = ranOnce || (state.config.recent_folders || []).length > 0;
  mark("st-run", ran && ok, ran ? "You have run an import or preview from this app." : "", ran ? "good" : "");
  const link = $("keyLink"); link.hidden = !srv; if (srv) link.href = c.server + "/user-settings?isOpen=api-keys";
  const left = [go, srv, key, ok].filter(x => !x).length;
  $("setupPip").hidden = left === 0;
  $("setupLine").textContent = left === 0 ? "Everything is set up. This page stays here for reference."
    : left === 1 ? "One step left before you can import." : left + " steps left before you can import.";
  $("tipCfg").textContent = "In " + state.config_file + ", which also holds the API key in plain text and is readable only by your account.";
}
document.addEventListener("click", async e => {
  const t = e.target.closest("[data-go], [data-copy], [data-copy-from]"); if (!t) return;
  if (t.dataset.go) { show(t.dataset.go); if (t.dataset.focus) $(t.dataset.focus).focus(); return; }
  const text = t.dataset.copy || $(t.dataset.copyFrom).textContent;
  try { await navigator.clipboard.writeText(text); t.textContent = "Copied"; }
  catch (err) { t.textContent = "Select and copy"; }
  setTimeout(() => { t.textContent = "Copy"; }, 1600);
});
$("setupTest").onclick = async () => {
  if (dirty && !(await save())) { show("settings"); return; }
  mark("st-test", false, "Testing…"); await checkServer();
};

/* ---- settings */
function fillForm(cfg) {
  for (const k of Object.keys(state.options)) {
    const sel = $(k); sel.innerHTML = "";
    const values = state.options[k].slice();
    if (!values.includes(cfg[k])) values.push(cfg[k]);
    for (const v of values) {
      const o = document.createElement("option"); o.value = v; o.textContent = (LABELS[k] && LABELS[k][v]) || v;
      sel.appendChild(o);
    }
  }
  for (const k of FIELDS) $(k).value = cfg[k];
  for (const k of CHECKS) $(k).checked = !!cfg[k];
  setDirty(false);
}
function readForm() {
  const cfg = Object.assign({}, state.config);
  for (const k of FIELDS) cfg[k] = $(k).value;
  for (const k of CHECKS) cfg[k] = $(k).checked;
  cfg.last_folder = folder || cfg.last_folder || "";
  return cfg;
}
function setDirty(on) {
  dirty = on; $("dirtyPip").hidden = !on; $("revert").hidden = !on;
  if (on) say($("saveInfo"), "You have unsaved changes.", "warn");
}
$("settingsForm").addEventListener("input", e => { if (e.target.id !== "showKey") setDirty(true); });
$("showKey").onclick = () => { const k = $("api_key"); const reveal = k.type === "password";
  k.type = reveal ? "text" : "password"; $("showKey").textContent = reveal ? "Hide" : "Show"; };
async function save() {
  let r;
  try { r = await api("/api/config", readForm()); }
  catch (e) { say($("saveInfo"), "Could not save. Is the app still running?", "bad"); return false; }
  if (!r.ok) { say($("saveInfo"), r.message, "bad"); return false; }
  state.config = r.config; state.immich_go = r.immich_go; fillForm(r.config);
  say($("saveInfo"), "Settings saved.", "good"); health(); updatePreview(); renderSetup(); checkServer(); return true;
}
$("save").onclick = save;
$("revert").onclick = () => { fillForm(state.config); say($("saveInfo"), "Changes discarded."); };
$("test").onclick = async () => {
  if (dirty && !(await save())) return;
  say($("testInfo"), "Testing…");
  const r = await checkServer(); say($("testInfo"), r.message, r.ok ? "good" : "bad");
};
async function checkServer() {
  $("srvDot").className = "dot busy"; $("srvText").textContent = "Checking Immich…";
  let r;
  try { r = await api("/api/test", {}); } catch (e) { r = {ok:false, message:"Could not check the connection."}; }
  $("srvDot").className = "dot " + (r.ok ? "good" : "bad");
  $("srvText").textContent = r.ok ? "Immich: " + r.message.replace(/\.$/, "").replace(/^Connected/, "connected")
    : "Immich is not connected. See Setup.";
  $("srvText").title = r.ok ? "" : r.message;
  conn = r; renderSetup();
  return r;
}
function health() {
  $("goDot").className = "dot " + (state.immich_go ? "good" : "bad");
  $("goText").textContent = state.immich_go ? "immich-go is installed" : "immich-go is missing. Install it with: brew install immich-go";
  $("goText").title = state.immich_go || "";
  $("cfgPath").textContent = "Saved in " + state.config_file + ", which also holds your API key." +
    (state.imported_from ? " First values came from " + state.imported_from + "." : "");
}

/* ---- sources */
function currentSource() { return selected === "__folder__" ? folder : selected; }
function describeSource() {
  const src = currentSource(); const line = $("sourceLine");
  if (!src) { line.textContent = "Choose what to import."; return; }
  const card = state.cards.find(c => c.path === src);
  const name = card ? card.name : src; const sc = scans[src];
  line.textContent = name + (sc && !sc.error ? ", " + sc.files.toLocaleString() + " photos and videos, " + fmtBytes(sc.bytes) : "");
}
function scan(path, target) {
  if (scans[path]) { if (target) target.textContent = countText(scans[path]); return; }
  api("/api/scan?path=" + encodeURIComponent(path)).then(x => {
    scans[path] = x; if (target) target.textContent = countText(x); describeSource();
  }).catch(() => { if (target) target.textContent = ""; });
}
function countText(x) { return x.error ? x.error : x.files.toLocaleString() + " photos and videos, " + fmtBytes(x.bytes); }
function renderCards() {
  const box = $("cards"); box.innerHTML = ""; box.style.display = "grid"; box.style.gap = "10px";
  if (!state.cards.length) {
    const e = el("div", "empty"); e.appendChild(el("b", "", "No camera card found"));
    e.appendChild(document.createTextNode("Insert a card, then look again. Or import from a folder."));
    box.appendChild(e);
  }
  for (const c of state.cards) {
    const lab = el("label", "tile");
    const r = document.createElement("input"); r.type = "radio"; r.name = "src"; r.value = c.path;
    const g = el("span", "glyph card"); g.setAttribute("aria-hidden", "true");
    const d = el("span");
    d.appendChild(el("span", "name", c.name));
    const cnt = el("span", "count", "Counting files…"); cnt.style.display = "block"; d.appendChild(cnt);
    if (c.folders.length) { const ch = el("span", "chips"); for (const f of c.folders) ch.appendChild(el("span", "chip", f)); d.appendChild(ch); }
    lab.append(r, g, d); box.appendChild(lab); scan(c.path, cnt);
  }
  const paths = state.cards.map(c => c.path);
  if (!selected || (selected !== "__folder__" && !paths.includes(selected))) {
    selected = paths.length ? paths[0] : (folder ? "__folder__" : null);
  }
  syncSelection();
}
function syncSelection() {
  document.querySelectorAll('input[name="src"]').forEach(r => {
    r.checked = r.value === selected; r.closest(".tile").classList.toggle("sel", r.checked);
  });
  $("folderPath").textContent = folder || "Any folder, USB drive or external disk";
  const fc = $("folderCount"); fc.hidden = !folder;
  if (folder) { fc.textContent = "Counting files…"; scan(folder, fc); }
  $("eject").hidden = !(selected && selected !== "__folder__");
  describeSource(); updatePreview();
}
document.addEventListener("change", e => {
  if (e.target.name !== "src") return;
  if (e.target.value === "__folder__" && !folder) { openPicker(state.config.last_folder || ""); }
  selected = e.target.value; syncSelection();
});
async function updatePreview() {
  if (!state) return;
  try { const r = await api("/api/preview", {source: currentSource() || "", dry_run: false}); $("cmd").textContent = r.command; }
  catch (e) { /* preview is optional */ }
}
async function openPicker(path) {
  let r;
  try { r = await api("/api/browse?path=" + encodeURIComponent(path || "")); }
  catch (e) { return; }
  pickAt = r.path; $("pickInput").value = r.path;
  const dlg = $("picker"); if (!dlg.open) dlg.showModal();
  const err = $("pickError"); err.hidden = !r.error; err.textContent = r.error || "";

  const crumbs = $("pickCrumbs"); crumbs.innerHTML = "";
  r.crumbs.forEach((c, i) => {
    if (i) crumbs.appendChild(el("i", "", "/"));
    const b = el("button", "", c.label); b.type = "button"; b.onclick = () => openPicker(c.path);
    if (i === r.crumbs.length - 1) b.setAttribute("aria-current", "location");
    crumbs.appendChild(b);
  });

  const places = $("pickPlaces"); places.innerHTML = ""; let lastKind = null;
  const row = (label, target, kind) => {
    const li = el("li"); const b = el("button"); b.type = "button"; b.title = target;
    b.append(el("i", "ico" + (kind === "card" ? " card" : kind === "drive" ? " drive" : "")), el("span", "", label));
    if (r.path === target) b.setAttribute("aria-current", "location");
    b.onclick = () => openPicker(target); li.appendChild(b); places.appendChild(li);
  };
  for (const pl of r.places) {
    const group = (pl.kind === "card" || pl.kind === "drive") ? "ext" : "local";
    if (lastKind && group !== lastKind) places.appendChild(el("li", "sep"));
    lastKind = group; row(pl.label, pl.path, pl.kind);
  }
  const recent = (state.config.recent_folders || []);
  if (recent.length) {
    places.appendChild(el("li", "sep"));
    for (const f of recent) row(f.split("/").filter(Boolean).pop() || f, f, "folder");
  }

  const ul = $("pickList"); ul.innerHTML = "";
  for (const d of r.dirs) {
    const li = el("li"); const b = el("button"); b.type = "button";
    b.append(el("i", "ico"), el("span", "", d));
    b.onclick = () => openPicker(r.path.replace(/\/$/, "") + "/" + d); li.appendChild(b); ul.appendChild(li);
  }
  if (!r.dirs.length && !r.error) ul.appendChild(el("li", "msg", "No folders inside this one."));
  const n = r.media_here;
  say($("pickInfo"), n === null ? "" : n ? n.toLocaleString() + (n >= 5000 ? "+" : "") + " photos and videos directly in this folder"
    : r.dirs.length ? "No photos here; its subfolders are included in the import" : "No photos or videos in this folder");
}
$("browseBtn").onclick = e => { e.preventDefault(); openPicker(folder || state.config.last_folder || ""); };
$("pickCancel").onclick = () => $("picker").close();
$("pickUse").onclick = () => { folder = pickAt; selected = "__folder__"; $("picker").close(); syncSelection(); };
$("pickGo").onclick = () => openPicker($("pickInput").value.trim());
$("pickForm").addEventListener("submit", e => { e.preventDefault(); openPicker($("pickInput").value.trim()); });
function renderQuick() {
  const box = $("quick"); box.innerHTML = "";
  const line = (title, items) => {
    if (!items.length) return;
    const row = el("div", "row"); row.appendChild(el("span", "", title));
    for (const it of items) { const b = el("button", "", it.label); b.type = "button"; b.title = it.path;
      b.onclick = it.pick ? () => { folder = it.path; selected = "__folder__"; syncSelection(); } : () => openPicker(it.path);
      row.appendChild(b); }
    box.appendChild(row);
  };
  line("Drives", (state.drives || []).map(d => ({label: d.name, path: d.path})));
  line("Recent", (state.config.recent_folders || []).map(f => ({label: f.split("/").filter(Boolean).pop() || f, path: f, pick: true})));
}
$("rescan").onclick = async () => {
  say($("scanInfo"), "Looking…");
  const found = await api("/api/cards"); state.cards = found.cards; state.drives = found.drives;
  for (const k of Object.keys(scans)) delete scans[k];
  renderCards(); renderQuick();
  const c = state.cards.length, d = state.drives.length;
  say($("scanInfo"), (c === 1 ? "1 card" : c + " cards") + " and " + (d === 1 ? "1 drive" : d + " drives") + " found.");
};

/* ---- running an import */
function setRunning(on) {
  running = on;
  $("run").hidden = on; $("dry").hidden = on; $("stop").hidden = !on; $("eject").disabled = on;
}
let sheetKey = "", feedKey = "";
const LEDGER = [["found_images","photos found"],["found_videos","videos found"],["uploaded","uploaded","go"],
  ["on_server","already in Immich"],["local_dupes","duplicates on the source"],["set_aside","set aside by settings"],
  ["stacked","stacked"],["tagged","tagged"],["albums","added to albums"],["upgraded","replaced with better copy"],
  ["metadata","metadata updated"],["live_photos","live photos"],["sidecars","sidecar files"],["ignored","not media"],
  ["errors","errors","bad"]];
const FEED_WORDS = {uploaded:"uploaded", upgraded:"replaced on server", on_server:"already in Immich",
  local_dupes:"duplicate on source", set_aside:"set aside", errors:"failed", stacked:"stacked"};
function fmtRate(bps) { return fmtBytes(bps) + "/s"; }
function series(points, key) {
  const out = [];
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i], dt = b.t - a.t;
    if (a[key] == null || b[key] == null || dt <= 0 || b[key] < a[key]) { out.push(null); continue; }
    out.push((b[key] - a[key]) / dt);
  }
  return out;
}
function smooth(v) { return v.map((x, i) => { const w = v.slice(Math.max(0, i - 2), i + 1).filter(y => y != null);
  return w.length ? w.reduce((a, b) => a + b, 0) / w.length : 0; }); }
function recent(v, n) { const w = v.slice(-n).filter(y => y != null); return w.length ? w.reduce((a, b) => a + b, 0) / w.length : null; }
function drawGeek(j, t) {
  const pts = j.points || [], bytes = series(pts, "bytes"), tx = series(pts, "tx"), files = series(pts, "files");
  const useBytes = j.bytes_up > 0, useTx = !useBytes && tx.some(x => x != null);
  const raw = useBytes ? bytes : useTx ? tx : files, v = smooth(raw);
  const box = $("chartBox"); box.hidden = v.length < 3;
  if (v.length >= 3) {
    const max = Math.max.apply(null, v) || 1, n = v.length, W = 600, H = 70;
    const xy = v.map((y, i) => [(i / (n - 1)) * W, H - 3 - (y / max) * (H - 10)]);
    const line = xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    $("chartLine").setAttribute("d", line); $("chartArea").setAttribute("d", line + " L" + W + " " + H + " L0 " + H + " Z");
    const now = recent(raw, 5) || 0, unit = x => (useBytes || useTx) ? fmtRate(x) : (x * 60).toFixed(0) + " files a minute";
    $("chartName").textContent = useBytes ? "Upload speed, by file size" : useTx ? "Network out, whole Mac" : "Upload pace";
    $("chartNow").textContent = j.running ? unit(now) : "";
    $("chartPeak").textContent = "peak " + unit(max) + ", last " + fmtTime(pts[pts.length - 1].t - pts[0].t);
  }
  const rows = [], row = (k, main, sub) => rows.push([k, main, sub]);
  if (j.bytes_up) row("Sent", fmtBytes(j.bytes_up), t && t.bytes ? Math.min(100, Math.round(100 * j.bytes_up / t.bytes)) + "% of " + fmtBytes(t.bytes) : "");
  const txNow = recent(tx, 5);
  if (txNow != null && tx.some(x => x)) row("Network out", (txNow * 8 / 1e6).toFixed(txNow * 8 / 1e6 < 10 ? 1 : 0) + " Mbit/s", (j.interface ? j.interface + ", " : "") + "all apps on this Mac");
  const sv = j.server || {}, s0 = j.server_start || {};
  if (sv.ping_ms != null) row("Server reply", sv.ping_ms + " ms", sv.version ? "Immich " + sv.version : "");
  if (j.process) row("immich-go", j.process.cpu.toFixed(0) + "% CPU", fmtBytes(j.process.mem) + " memory");
  if (sv.assets != null) row("In your Immich", sv.assets.toLocaleString(), s0.assets != null && sv.assets !== s0.assets ? (sv.assets > s0.assets ? "+" : "") + (sv.assets - s0.assets).toLocaleString() + " this run" : "photos and videos");
  if (sv.disk_size) row("Server disk", fmtBytes(sv.disk_used) + " used", s0.disk_used != null && sv.disk_used > s0.disk_used ? "+" + fmtBytes(sv.disk_used - s0.disk_used) + " this run" : "of " + fmtBytes(sv.disk_size));
  const g = $("geek"); g.innerHTML = "";
  for (const [k, main, sub] of rows) { const d = el("div"); d.appendChild(el("dt", "", k));
    const dd2 = el("dd", "", main); if (sub) dd2.appendChild(el("small", "", sub)); d.appendChild(dd2); g.appendChild(d); }

  const c = j.counters || {}, led = $("ledger"); led.innerHTML = "";
  for (const [key, label, cls] of LEDGER) { if (!c[key]) continue;
    const chip = el("span", cls || ""); chip.appendChild(el("b", "", c[key].toLocaleString()));
    chip.append(c[key] === 1 ? label.replace(/^(photo|video|error|duplicate|live photo|sidecar file)s\b/, "$1") : label); led.appendChild(chip); }

  const feed = (j.feed || []).slice(-6).reverse(), key = feed.map(f => f.kind + f.file).join("|");
  if (key !== feedKey) { feedKey = key; const ol = $("feed"); ol.innerHTML = "";
    for (const f of feed) { const li = el("li", f.kind === "errors" ? "bad" : (f.kind === "uploaded" || f.kind === "upgraded") ? "" : "skip");
      li.append(el("i"), el("span", "", f.file.split("/").pop()), el("small", "", (FEED_WORDS[f.kind] || f.what) + (f.size ? ", " + fmtBytes(f.size) : "")));
      li.title = f.file; ol.appendChild(li); } }
}
function fmtDay(t) { return new Date(t * 1000).toLocaleDateString(undefined, {day:"numeric", month:"short", year:"numeric"}); }
function buildSheet(samples) {
  const key = samples.join("|"); if (key === sheetKey) return; sheetKey = key;
  const box = $("sheet"); box.innerHTML = "";
  for (const path of samples) {
    const f = el("div", "frame"); const img = new Image(); img.alt = ""; img.loading = "lazy";
    img.src = "/api/thumb?t=" + encodeURIComponent(TOKEN) + "&path=" + encodeURIComponent(path);
    img.onerror = () => { img.remove(); f.appendChild(el("span", "", path.split("/").pop())); };
    f.appendChild(img); box.appendChild(f);
  }
}
function showProgress(j) {
  if (!j.started) return;
  $("prog").hidden = false; $("rest").hidden = true;
  const p = j.progress, t = j.totals, kind = j.dry_run ? "Preview" : "Import";
  const found = p ? p.found : 0, done = p ? p.uploaded + p.errors : 0;
  const ok = !j.running && j.returncode === 0 && !j.stopped;
  let pct = found ? Math.floor(100 * done / found) : 0;
  if (j.running) pct = Math.min(pct, 99); if (ok) pct = 100;
  const elapsed = (j.running ? Date.now() / 1000 : (j.finished || Date.now() / 1000)) - j.started;

  let phase;
  if (j.running) {
    if (!p) phase = "Starting immich-go";
    else if (p.immich_read < 100 && !p.uploaded) phase = "Reading your Immich library, " + p.immich_read + "%";
    else phase = (j.dry_run ? "Previewing" : "Uploading") + (j.current ? " " + j.current.split("/").pop() : "");
  } else phase = j.stopped ? kind + " stopped" : ok ? kind + " finished" : kind + " failed";
  $("pPhase").textContent = phase; $("pPct").textContent = (p || !j.running) ? pct + "%" : "";
  const bar = $("pBarBox");
  bar.className = "bar" + (j.running && !found ? " wait" : ok ? " done" : (!j.running && !j.stopped ? " fail" : ""));
  $("pBar").style.width = (j.running && !found) ? "" : pct + "%"; bar.setAttribute("aria-valuenow", pct);
  document.title = j.running ? (p ? pct + "% " : "") + "my_immich" : "my_immich";

  const dd = (id, main, sub, cls) => { const e = $(id); e.textContent = main; e.className = cls || "";
    if (sub) e.appendChild(el("small", "", sub)); };
  dd("pDone", p ? p.uploaded.toLocaleString() + " of " + found.toLocaleString() : (t ? "0 of " + t.files.toLocaleString() : "0"),
     p && p.errors ? p.errors + (p.errors === 1 ? " error" : " errors") : (j.dry_run ? "nothing is sent" : ""), p && p.errors ? "bad" : "");
  const left = found - done;
  if (!j.running) dd("pLeft", ok ? "None" : left > 0 ? left.toLocaleString() + " files" : "None", ok ? "" : left > 0 ? "not done" : "");
  else if (j.rate && left > 0) dd("pLeft", "About " + fmtTime(left / j.rate), left.toLocaleString() + " files to go");
  else dd("pLeft", "Working it out");
  const avg = t && t.files ? t.bytes / t.files : 0;
  if (j.rate) dd("pRate", Math.max(1, Math.round(j.rate * 60)).toLocaleString() + " a minute",
    j.bytes_up ? "" : avg ? "about " + fmtBytes(j.rate * avg) + "/s" : "");
  else if (!j.running && p && elapsed > 0 && p.uploaded) dd("pRate", Math.max(1, Math.round(p.uploaded / elapsed * 60)).toLocaleString() + " a minute", "on average");
  else dd("pRate", j.running ? "Working it out" : "None");
  dd("pTime", fmtTime(elapsed));

  drawGeek(j, t);
  if (j.samples && j.samples.length) buildSheet(j.samples);
  const frames = $("sheet").children, lit = ok ? frames.length : Math.floor(frames.length * pct / 100);
  for (let i = 0; i < frames.length; i++) frames[i].classList.toggle("dev", i < lit);

  const bits = [];
  if (t && t.files) {
    const kinds = [];
    if (t.photos) kinds.push(t.photos.toLocaleString() + (t.photos === 1 ? " photo" : " photos") + (t.raw ? " (" + t.raw.toLocaleString() + " RAW)" : ""));
    if (t.videos) kinds.push(t.videos.toLocaleString() + (t.videos === 1 ? " video" : " videos"));
    bits.push("This source holds " + kinds.join(" and ") + ", " + fmtBytes(t.bytes) + ".");
    if (t.oldest && t.newest) { const a = fmtDay(t.oldest), b = fmtDay(t.newest);
      bits.push(a === b ? "All files are dated " + a + "." : "File dates run from " + a + " to " + b + "."); }
  }
  const c = j.counters || {};
  if (!j.running && ok && (c.on_server || c.local_dupes || c.set_aside)) {
    const parts = [];
    if (c.on_server) parts.push(c.on_server.toLocaleString() + " already in Immich");
    if (c.local_dupes) parts.push(c.local_dupes.toLocaleString() + " duplicated on the source");
    if (c.set_aside) parts.push(c.set_aside.toLocaleString() + " set aside by your settings");
    bits.push("Left out: " + parts.join(", ") + ".");
  } else if (!j.running && p) {
    const skipped = Math.max(0, p.found - p.uploaded - p.errors);
    if (ok && skipped) bits.push(skipped.toLocaleString() + (skipped === 1 ? " file was" : " files were") +
      (j.dry_run ? " left out: already in Immich, or set aside by your settings." : " not uploaded: already in Immich, or set aside by your settings."));
  }
  $("pNote").textContent = bits.join(" ");
}
async function start(dry) {
  const src = currentSource();
  if (!src) { say($("runInfo"), "Choose a card or a folder first.", "bad"); return; }
  if (dirty && !(await save())) { show("settings"); return; }
  let r;
  try { r = await api("/api/run", {source: src, dry_run: dry}); }
  catch (e) { say($("runInfo"), "Could not start. Is the app still running?", "bad"); return; }
  if (!r.ok) { say($("runInfo"), r.message, "bad"); return; }
  if (selected === "__folder__") {
    state.config.recent_folders = [src].concat((state.config.recent_folders || []).filter(f => f !== src)).slice(0, 6);
    state.config.last_folder = src; renderQuick();
  }
  const log = $("log"); log.textContent = ""; log.classList.remove("idle"); logNext = 0;
  sheetKey = ""; feedKey = ""; $("sheet").innerHTML = ""; $("feed").innerHTML = ""; $("logBox").open = false;
  setRunning(true); ranOnce = true; renderSetup();
  showProgress({running:true, started:Date.now() / 1000, dry_run:dry, progress:null, totals:scans[src] || null, samples:[],
    points:[], counters:{}, feed:[], bytes_up:0});
  say($("runInfo"), dry ? "Previewing. Nothing is being uploaded." : "Importing to Immich."); poll();
}
$("dry").onclick = () => start(true);
$("run").onclick = () => start(false);
$("stop").onclick = async () => { await api("/api/stop", {}); say($("runInfo"), "Stopping…", "warn"); };
$("eject").onclick = async () => {
  const r = await api("/api/eject", {path: currentSource()});
  say($("runInfo"), r.message, r.ok ? "good" : "bad");
  if (r.ok) $("rescan").click();
};
async function poll() {
  clearTimeout(polling);
  let j;
  try { j = await api("/api/log?since=" + logNext); }
  catch (e) { say($("runInfo"), "Lost contact with my_immich. Start it again from Applications.", "bad"); setRunning(false); return; }
  if (j.lines.length) {
    const log = $("log"); log.classList.remove("idle");
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent += (log.textContent ? "\n" : "") + j.lines.join("\n");
    if (atEnd) log.scrollTop = log.scrollHeight;
  }
  logNext = j.next;
  showProgress(j);
  if (j.running) { if (!running) setRunning(true); polling = setTimeout(poll, 1000); return; }
  setRunning(false);
  if (j.returncode === null) return;
  const took = j.finished && j.started ? " in " + fmtTime(j.finished - j.started) : "";
  if (j.stopped) say($("runInfo"), (j.dry_run ? "Preview" : "Import") + " stopped. Run it again to carry on where it left off.", "warn");
  else if (j.returncode === 0) say($("runInfo"), j.dry_run ? "Preview finished" + took + ". Nothing was uploaded." : "Import finished" + took + ". Check Immich before formatting the card.", "good");
  else { say($("runInfo"), (j.dry_run ? "Preview" : "Import") + " failed. The last lines of the log say why.", "bad");
    $("logBox").open = true; const log = $("log"); log.scrollTop = log.scrollHeight; }
}

(async function init() {
  try { state = await api("/api/state"); }
  catch (e) { $("sourceLine").textContent = "Could not load. Quit my_immich and open it again."; return; }
  folder = state.config.last_folder || "";
  fillForm(state.config); say($("saveInfo"), ""); health(); renderCards(); renderQuick();
  const want = location.hash.slice(1), ready = state.immich_go && state.config.server && state.config.api_key;
  show(!ready ? "setup" : (want === "settings" || want === "setup") ? want : "import");
  renderSetup();
  if (state.config.server) checkServer(); else { $("srvDot").className = "dot bad"; $("srvText").textContent = "Immich is not set up yet. Open Setup."; }
  if (state.job.running || state.job.returncode !== null) { $("log").textContent = ""; logNext = 0; poll(); }
})();
</script>
</body>
</html>
"""


def already_running(port):
    """True if another copy of this app is serving on that port."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/hello" % port, timeout=1.5) as resp:
            return json.loads(resp.read().decode()).get("app") == APP_ID
    except Exception:  # noqa: BLE001 - nothing there, or not us
        return False


def run_tray(url):
    """Menu-bar icon (macOS). Needs the `rumps` package; returns False if it is missing."""
    try:
        import rumps
    except ImportError:
        return False

    try:  # keep the Python rocket out of the Dock
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["LSUIElement"] = "1"
    except Exception:  # noqa: BLE001
        pass

    def notify(title, message):
        try:
            rumps.notification(APP_NAME, title, message)
        except Exception:  # noqa: BLE001 - notifications are a nicety
            pass

    class Tray(rumps.App):
        def __init__(self):
            # A template image follows the menu bar's light/dark colour; text is the fallback.
            self.has_icon = MENUBAR_ICON.is_file()
            if self.has_icon:
                super().__init__(APP_NAME, title=None, icon=str(MENUBAR_ICON), template=True, quit_button=None)
            else:
                super().__init__(APP_NAME, title="MI", quit_button=None)
            self.card_item = rumps.MenuItem("No camera card inserted")
            self.job_item = rumps.MenuItem("Idle")
            self.menu = [rumps.MenuItem("Open my_immich", callback=self.open_page), None,
                         self.card_item, self.job_item, None,
                         rumps.MenuItem("Quit", callback=self.quit_app)]
            self.known_cards = None
            self.was_running = False
            self.timer = rumps.Timer(self.tick, 4)
            self.timer.start()

        def open_page(self, _=None):
            webbrowser.open(url)

        def tick(self, _):
            names = [c["name"] for c in list_cards()]
            self.card_item.title = ("Card: " + ", ".join(names)) if names else "No camera card inserted"
            if self.known_cards is not None:
                for name in names:
                    if name not in self.known_cards:
                        notify("Camera card inserted", "%s is ready to import." % name)
            self.known_cards = names
            job = JOB.snapshot(10 ** 12)
            if job["running"]:
                self.title = "↑" if self.has_icon else "MI ↑"
                self.job_item.title = "Dry run in progress…" if job["dry_run"] else "Importing…"
            else:
                self.title = None if self.has_icon else "MI"
                if self.was_running:
                    kind = "Dry run" if job["dry_run"] else "Import"
                    if job["stopped"]:
                        outcome = "stopped"
                    elif job["returncode"] == 0:
                        outcome = "finished"
                    else:
                        outcome = "failed (exit code %s)" % job["returncode"]
                    self.job_item.title = "%s %s" % (kind, outcome)
                    notify("%s %s" % (kind, outcome), job["source"])
            self.was_running = job["running"]

        def quit_app(self, _):
            if JOB.snapshot(10 ** 12)["running"]:
                answer = rumps.alert(APP_NAME, "An import is running. Quitting will stop it.",
                                     ok="Quit and stop", cancel="Keep running")
                if answer != 1:
                    return
                JOB.stop()
            rumps.quit_application()

    Tray().run()
    return True


def relaunch_for_tray(args):
    """Restart this process without the script path on its command line.

    A menu-bar app is a Cocoa application, and Cocoa treats a file path among
    the process arguments as a document to open. With `python my_immich.py
    --tray` that document is my_immich.py itself, which macOS then hands to the
    default editor for .py files (Xcode). Starting Python with `-c` and passing
    the options through the environment leaves no file path for Cocoa to find.
    Does nothing off macOS, when rumps is missing, or in the restarted process.
    """
    if sys.platform != "darwin" or os.environ.get("MY_IMMICH_TRAY_CHILD") == "1":
        return
    try:
        import importlib.util
        if importlib.util.find_spec("rumps") is None:
            return
    except Exception:  # noqa: BLE001
        return
    here = str(Path(__file__).resolve().parent)
    boot = "import sys; sys.path.insert(0, %r); import my_immich; my_immich.main()" % here
    env = dict(os.environ, MY_IMMICH_TRAY_CHILD="1", MY_IMMICH_PORT=str(args.port),
               MY_IMMICH_NO_BROWSER="1" if args.no_browser else "0")
    os.chdir(str(Path.home()))
    os.execve(sys.executable, [sys.executable, "-c", boot], env)


def main():
    parser = argparse.ArgumentParser(description="Local web GUI for importing camera cards into Immich.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    parser.add_argument("--tray", action="store_true", help="show a menu-bar icon (macOS, needs rumps)")
    args = parser.parse_args()
    if os.environ.get("MY_IMMICH_TRAY_CHILD") == "1":      # see relaunch_for_tray
        args.tray = True
        args.port = int(os.environ.get("MY_IMMICH_PORT") or args.port)
        args.no_browser = os.environ.get("MY_IMMICH_NO_BROWSER") == "1"

    if already_running(args.port):
        url = "http://127.0.0.1:%d/" % args.port
        print("%s is already running at %s" % (APP_NAME, url))
        if not args.no_browser:
            webbrowser.open(url)
        return

    if args.tray:
        relaunch_for_tray(args)

    STATE["config"], STATE["imported_from"] = load_config()

    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit("No free port found near %d." % args.port)
    httpd.daemon_threads = True
    STATE["port"] = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % STATE["port"]
    print("%s is running at %s  (Ctrl+C to quit)" % (APP_NAME, url))
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        if args.tray:
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            if run_tray(url):
                return
            print("Menu-bar icon unavailable (the 'rumps' package is not installed); running without it.")
            while True:
                time.sleep(3600)
        else:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        JOB.stop()
        httpd.shutdown() if args.tray else None
        httpd.server_close()


if __name__ == "__main__":
    main()
