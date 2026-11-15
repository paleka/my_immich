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
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
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
}
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
    cfg = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in data:
            continue
        value = data[key]
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


def scan_folder(path):
    count = size = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in MEDIA_EXT:
                continue
            count += 1
            try:
                size += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return {"files": count, "bytes": size}


def browse(path):
    p = Path(path or Path.home()).expanduser()
    if not p.is_dir():
        p = Path.home()
    p = p.resolve()
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
    except OSError as exc:
        return {"path": str(p), "parent": str(p.parent), "dirs": [], "error": str(exc)}
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs,
            "shortcuts": [{"label": "Home", "path": str(Path.home())},
                          {"label": "Volumes", "path": str(VOLUMES_ROOT)}]}


def test_connection(cfg):
    server = cfg["server"]
    if not server:
        return {"ok": False, "message": "Enter the Immich server address first."}

    def get(url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode() or "{}")

    try:
        get(server + "/api/server/ping")
    except Exception as exc:  # noqa: BLE001 - shown to the user as-is
        return {"ok": False, "message": "Cannot reach %s (%s)." % (server, exc)}
    if not cfg["api_key"]:
        return {"ok": False, "message": "Server is reachable, but no API key is set."}
    try:
        me = get(server + "/api/users/me", {"x-api-key": cfg["api_key"], "Accept": "application/json"})
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "message": "Server reachable, but the API key was refused (HTTP %s). %s"
                % (exc.code, detail)}
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


class Job:
    MAX_LINES = 20000

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
        run_cmd = list(cmd)
        if cfg["keep_awake"] and shutil.which("caffeinate"):
            run_cmd = ["caffeinate", "-i"] + run_cmd
        key = cfg["api_key"]
        with self.lock:
            self.lines, self.offset = [], 0
            self.running, self.returncode, self.stopped = True, None, False
            self.source, self.dry_run = source, dry_run
            self.started, self.finished, self.note = time.time(), None, ""
        self.add("$ " + mask(" ".join(shlex.quote(c) for c in cmd), key))
        try:
            self.proc = subprocess.Popen(run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1)
        except OSError as exc:
            with self.lock:
                self.running, self.returncode, self.finished = False, -1, time.time()
            self.add("Could not start immich-go: %s" % exc)
            return None
        threading.Thread(target=self._pump, args=(cfg, key), daemon=True).start()
        return None

    def _pump(self, cfg, key):
        proc = self.proc
        for line in proc.stdout:
            self.add(mask(line.rstrip("\n"), key))
        code = proc.wait()
        proc.stdout.close()
        note = ""
        if code == 0 and not self.dry_run and not self.stopped and cfg["eject_after"]:
            note = eject_for(self.source)
        with self.lock:
            self.running, self.returncode, self.finished, self.note = False, code, time.time(), note
        if note:
            self.add(note)

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
                    "lines": self.lines[start:]}


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
        if not self._auth():
            return None
        cfg = STATE["config"]
        if url.path == "/api/state":
            return self._send(200, {
                "config": cfg, "cards": list_cards(), "immich_go": find_immich_go(cfg),
                "config_file": str(CONFIG_FILE), "imported_from": STATE["imported_from"],
                "options": ENUMS, "job": JOB.snapshot(10 ** 12)})
        if url.path == "/api/cards":
            return self._send(200, {"cards": list_cards()})
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
            cfg = clean_config(body)
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
            if volume_of(source) is None and cfg.get("last_folder") != source:
                cfg["last_folder"] = source
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
<style>
  :root {
    --bg:#f4f5f7; --panel:#fff; --ink:#16181d; --muted:#667085; --line:#dfe3ea;
    --accent:#3457d5; --accent-ink:#fff; --ok:#12805c; --bad:#c0362c; --warn:#a15c07;
    --field:#fff; --log:#0f1218; --log-ink:#d7dce5;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#101216; --panel:#181b21; --ink:#e8eaf0; --muted:#98a2b3; --line:#2a2f39;
      --accent:#7c96ff; --accent-ink:#0d1020; --ok:#4cc79a; --bad:#ff7b72; --warn:#e0a44a;
      --field:#12151a; --log:#0b0d11; --log-ink:#cfd5df; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  header { padding:20px 24px 4px; max-width:1180px; margin:0 auto; display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:22px; margin:0; letter-spacing:-.01em; }
  header .sub { color:var(--muted); font-size:13px; }
  main { max-width:1180px; margin:0 auto; padding:14px 24px 40px; display:grid; gap:18px;
    grid-template-columns:minmax(0,1fr) minmax(0,1fr); }
  @media (max-width:900px) { main { grid-template-columns:1fr; } }
  section { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:18px 20px; }
  section.wide { grid-column:1 / -1; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 12px; }
  label.row { display:grid; grid-template-columns:170px minmax(0,1fr); gap:12px; align-items:center; margin:9px 0; }
  label.row > span { color:var(--muted); font-size:13.5px; }
  input[type=text], input[type=password], select {
    width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px;
    background:var(--field); color:var(--ink); font:inherit; }
  input:focus, select:focus, button:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .checks { display:grid; grid-template-columns:1fr 1fr; gap:6px 16px; margin:12px 0 4px; }
  .checks label { display:flex; gap:8px; align-items:flex-start; font-size:14px; }
  .hint { color:var(--muted); font-size:12.5px; margin:4px 0 0; }
  button { font:inherit; border-radius:8px; border:1px solid var(--line); background:var(--field);
    color:var(--ink); padding:8px 14px; cursor:pointer; }
  button.primary { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); font-weight:600; }
  button.danger { color:var(--bad); border-color:var(--bad); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .bar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:12px; }
  .status { font-size:13.5px; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); } .muted { color:var(--muted); }
  .src { display:flex; gap:12px; align-items:flex-start; border:1px solid var(--line); border-radius:10px;
    padding:12px 14px; margin:8px 0; cursor:pointer; }
  .src.sel { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset; }
  .src input { margin-top:3px; }
  .src .name { font-weight:600; }
  .src .meta { color:var(--muted); font-size:13px; overflow-wrap:anywhere; }
  .badge { display:inline-block; font-size:11px; font-weight:700; letter-spacing:.05em; padding:1px 7px;
    border-radius:99px; background:var(--accent); color:var(--accent-ink); margin-left:6px; vertical-align:2px; }
  .picker { border:1px solid var(--line); border-radius:10px; margin-top:8px; overflow:hidden; }
  .picker .path { padding:8px 12px; font-size:13px; border-bottom:1px solid var(--line); display:flex; gap:8px;
    align-items:center; flex-wrap:wrap; overflow-wrap:anywhere; }
  .picker ul { list-style:none; margin:0; padding:4px 0; max-height:230px; overflow:auto; }
  .picker li button { width:100%; text-align:left; border:0; border-radius:0; background:none; padding:6px 14px; }
  .picker li button:hover { background:var(--bg); }
  pre { margin:0; font:12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space:pre-wrap; overflow-wrap:anywhere; }
  #cmd { background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:10px 12px; color:var(--muted); }
  #log { background:var(--log); color:var(--log-ink); border-radius:10px; padding:12px 14px; height:340px; overflow:auto; margin-top:12px; }
  details summary { cursor:pointer; color:var(--muted); font-size:13.5px; margin-top:10px; }
</style>
</head>
<body>
<header>
  <h1>my_immich</h1>
  <span class="sub" id="envline">Loading…</span>
</header>
<main>
  <section>
    <h2>Source</h2>
    <div id="cards"></div>
    <label class="src" id="folderSrc">
      <input type="radio" name="src" value="__folder__">
      <div style="flex:1; min-width:0">
        <div class="name">Other folder</div>
        <div class="meta" id="folderPath">No folder chosen</div>
        <div class="bar" style="margin-top:8px">
          <button type="button" id="browseBtn">Choose folder…</button>
        </div>
        <div class="picker" id="picker" hidden>
          <div class="path"><span id="pickPath"></span></div>
          <ul id="pickList"></ul>
          <div class="path" style="border-top:1px solid var(--line); border-bottom:0">
            <button type="button" class="primary" id="pickUse">Use this folder</button>
            <button type="button" id="pickCancel">Cancel</button>
            <span id="pickShort"></span>
          </div>
        </div>
      </div>
    </label>
    <div class="bar">
      <button type="button" id="rescan">Rescan for cards</button>
      <span class="status muted" id="scanInfo"></span>
    </div>
  </section>

  <section>
    <h2>Settings</h2>
    <label class="row"><span>Immich server</span><input type="text" id="server" placeholder="http://192.168.1.10:2283" spellcheck="false"></label>
    <label class="row"><span>API key</span>
      <span style="display:flex; gap:8px"><input type="password" id="api_key" autocomplete="off" spellcheck="false">
      <button type="button" id="showKey">Show</button></span></label>
    <label class="row"><span>RAW + JPEG pairs</span><select id="raw_jpeg"></select></label>
    <label class="row"><span>Bursts</span><select id="burst"></select></label>
    <label class="row"><span>If a file fails</span><select id="on_errors"></select></label>
    <div class="checks">
      <label><input type="checkbox" id="session_tag"> Tag each import with its time</label>
      <label><input type="checkbox" id="pause_jobs"> Pause Immich jobs while uploading</label>
      <label><input type="checkbox" id="keep_awake"> Keep the Mac awake</label>
      <label><input type="checkbox" id="eject_after"> Eject the card when done</label>
    </div>
    <details>
      <summary>More options</summary>
      <label class="row"><span>HEIC + JPEG pairs</span><select id="heic_jpeg"></select></label>
      <label class="row"><span>Albums from folders</span><select id="folder_as_album"></select></label>
      <label class="row"><span>Put all into album</span><input type="text" id="into_album" placeholder="leave empty for none"></label>
      <label class="row"><span>Upload</span><select id="include_type"></select></label>
      <label class="row"><span>immich-go path</span><input type="text" id="immich_go_path" placeholder="found automatically" spellcheck="false"></label>
      <label class="row"><span>Extra arguments</span><input type="text" id="extra_args" placeholder="e.g. --date-range=2026" spellcheck="false"></label>
      <div class="checks"><label><input type="checkbox" id="recursive"> Include subfolders</label></div>
    </details>
    <div class="bar">
      <button type="button" class="primary" id="save">Save settings</button>
      <button type="button" id="test">Test connection</button>
      <span class="status" id="saveInfo"></span>
    </div>
    <p class="hint" id="cfgPath"></p>
  </section>

  <section class="wide">
    <h2>Import</h2>
    <pre id="cmd">—</pre>
    <div class="bar">
      <button type="button" id="dry">Preview (dry run)</button>
      <button type="button" class="primary" id="run">Import to Immich</button>
      <button type="button" class="danger" id="stop" disabled>Stop</button>
      <button type="button" id="eject" hidden>Eject card</button>
      <span class="status" id="runInfo"></span>
    </div>
    <pre id="log" aria-live="polite">Nothing has run yet.</pre>
  </section>
</main>

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
let state = null, selected = null, folder = "", logNext = 0, polling = null, pickAt = "", dirty = false;

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
function setInfo(el, text, cls) { el.textContent = text; el.className = "status " + (cls || "muted"); }

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
  dirty = false;
}
function readForm() {
  const cfg = Object.assign({}, state.config);
  for (const k of FIELDS) cfg[k] = $(k).value;
  for (const k of CHECKS) cfg[k] = $(k).checked;
  cfg.last_folder = folder || cfg.last_folder || "";
  return cfg;
}
function currentSource() { return selected === "__folder__" ? folder : selected; }

function renderCards() {
  const box = $("cards"); box.innerHTML = "";
  if (!state.cards.length) {
    const p = document.createElement("p"); p.className = "hint";
    p.textContent = "No camera card found. Insert one and press Rescan, or choose a folder below.";
    box.appendChild(p);
  }
  for (const c of state.cards) {
    const lab = document.createElement("label"); lab.className = "src";
    const r = document.createElement("input"); r.type = "radio"; r.name = "src"; r.value = c.path;
    const d = document.createElement("div"); d.style.minWidth = "0";
    const n = document.createElement("div"); n.className = "name"; n.textContent = c.name;
    const b = document.createElement("span"); b.className = "badge"; b.textContent = "SD CARD"; n.appendChild(b);
    const m = document.createElement("div"); m.className = "meta";
    m.textContent = c.path + (c.folders.length ? " · " + c.folders.join(", ") : "");
    const s = document.createElement("div"); s.className = "meta"; s.dataset.scan = c.path; s.textContent = "Counting files…";
    d.append(n, m, s); lab.append(r, d); box.appendChild(lab);
    api("/api/scan?path=" + encodeURIComponent(c.path)).then(x => {
      s.textContent = x.error ? x.error : x.files.toLocaleString() + " photos and videos · " + fmtBytes(x.bytes);
    }).catch(() => { s.textContent = ""; });
  }
  const paths = state.cards.map(c => c.path);
  if (!selected || (selected !== "__folder__" && !paths.includes(selected))) {
    selected = paths.length ? paths[0] : (folder ? "__folder__" : null);
  }
  syncSelection();
}
function syncSelection() {
  document.querySelectorAll('input[name="src"]').forEach(r => {
    r.checked = r.value === selected;
    r.closest(".src").classList.toggle("sel", r.checked);
  });
  $("folderPath").textContent = folder || "No folder chosen";
  const onCard = selected && selected !== "__folder__";
  $("eject").hidden = !onCard;
  updatePreview();
}
document.addEventListener("change", e => {
  if (e.target.name === "src") { selected = e.target.value; syncSelection(); }
});

async function updatePreview() {
  if (!state) return;
  try {
    const r = await api("/api/preview", {source: currentSource() || "", dry_run: false});
    $("cmd").textContent = r.command + (dirty ? "\n(unsaved settings changes are not included yet)" : "");
  } catch (e) { /* ignore */ }
}

async function openPicker(path) {
  const r = await api("/api/browse?path=" + encodeURIComponent(path || ""));
  pickAt = r.path; $("picker").hidden = false; $("pickPath").textContent = r.path;
  const ul = $("pickList"); ul.innerHTML = "";
  const add = (label, target) => {
    const li = document.createElement("li"); const b = document.createElement("button");
    b.type = "button"; b.textContent = label; b.onclick = () => openPicker(target); li.appendChild(b); ul.appendChild(li);
  };
  if (r.parent) add("↑ Up one level", r.parent);
  for (const d of r.dirs) add(d, r.path.replace(/\/$/, "") + "/" + d);
  if (!r.dirs.length) { const li = document.createElement("li"); li.className = "hint"; li.style.padding = "6px 14px";
    li.textContent = r.error || "No subfolders here."; ul.appendChild(li); }
  const sc = $("pickShort"); sc.innerHTML = "";
  for (const s of (r.shortcuts || [])) { const b = document.createElement("button"); b.type = "button";
    b.textContent = s.label; b.onclick = () => openPicker(s.path); sc.appendChild(b); sc.append(" "); }
}
$("browseBtn").onclick = e => { e.preventDefault(); openPicker(folder || state.config.last_folder || ""); };
$("pickCancel").onclick = e => { e.preventDefault(); $("picker").hidden = true; };
$("pickUse").onclick = e => { e.preventDefault(); folder = pickAt; selected = "__folder__"; $("picker").hidden = true; syncSelection(); };

$("rescan").onclick = async () => {
  setInfo($("scanInfo"), "Looking…");
  state.cards = (await api("/api/cards")).cards; renderCards();
  setInfo($("scanInfo"), state.cards.length ? state.cards.length + " card(s) found." : "No card found.");
};
$("showKey").onclick = () => { const k = $("api_key"); const show = k.type === "password";
  k.type = show ? "text" : "password"; $("showKey").textContent = show ? "Hide" : "Show"; };

async function save() {
  const r = await api("/api/config", readForm());
  if (!r.ok) { setInfo($("saveInfo"), r.message, "bad"); return false; }
  state.config = r.config; state.immich_go = r.immich_go; fillForm(r.config); envLine();
  setInfo($("saveInfo"), "Saved.", "ok"); updatePreview(); return true;
}
$("save").onclick = save;
$("test").onclick = async () => {
  if (!(await save())) return;
  setInfo($("saveInfo"), "Testing…");
  const r = await api("/api/test", {}); setInfo($("saveInfo"), r.message, r.ok ? "ok" : "bad");
};
document.querySelector("section:nth-of-type(2)").addEventListener("input", () => {
  dirty = true; setInfo($("saveInfo"), "Unsaved changes.", "warn"); });

function envLine() {
  const parts = [];
  parts.push(state.immich_go ? "immich-go: " + state.immich_go : "immich-go not found — brew install immich-go");
  $("envline").textContent = parts.join(" · ");
  $("envline").className = "sub" + (state.immich_go ? "" : " bad");
  $("cfgPath").textContent = "Settings file: " + state.config_file + " (contains your API key; readable by you only)" +
    (state.imported_from ? " · first-run values were read from " + state.imported_from : "");
}

function setRunning(on) {
  $("run").disabled = on; $("dry").disabled = on; $("stop").disabled = !on; $("eject").disabled = on;
}
async function start(dry) {
  const src = currentSource();
  if (!src) { setInfo($("runInfo"), "Choose a card or folder first.", "bad"); return; }
  if (dirty && !(await save())) return;
  const r = await api("/api/run", {source: src, dry_run: dry});
  if (!r.ok) { setInfo($("runInfo"), r.message, "bad"); return; }
  $("log").textContent = ""; logNext = 0; setRunning(true);
  setInfo($("runInfo"), dry ? "Dry run in progress…" : "Importing…"); poll();
}
$("dry").onclick = () => start(true);
$("run").onclick = () => start(false);
$("stop").onclick = async () => { await api("/api/stop", {}); setInfo($("runInfo"), "Stopping…", "warn"); };
$("eject").onclick = async () => {
  const r = await api("/api/eject", {path: currentSource()});
  setInfo($("runInfo"), r.message, r.ok ? "ok" : "bad");
  if (r.ok) $("rescan").click();
};

async function poll() {
  clearTimeout(polling);
  let j;
  try { j = await api("/api/log?since=" + logNext); }
  catch (e) { setInfo($("runInfo"), "Lost contact with the app. Is it still running?", "bad"); setRunning(false); return; }
  if (j.lines.length) {
    const log = $("log"); const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent += (log.textContent ? "\n" : "") + j.lines.join("\n");
    if (atEnd) log.scrollTop = log.scrollHeight;
  }
  logNext = j.next;
  if (j.running) { setRunning(true); polling = setTimeout(poll, 1000); return; }
  setRunning(false);
  if (j.returncode === null) return;
  const what = j.dry_run ? "Dry run" : "Import";
  if (j.stopped) setInfo($("runInfo"), what + " stopped. Run it again to pick up where it left off.", "warn");
  else if (j.returncode === 0) setInfo($("runInfo"), what + " finished" + (j.dry_run ? " — nothing was uploaded." : "."), "ok");
  else setInfo($("runInfo"), what + " failed (exit code " + j.returncode + "). See the log.", "bad");
}

(async function init() {
  try { state = await api("/api/state"); }
  catch (e) { $("envline").textContent = "Could not load. Restart the app and use the page it opens."; return; }
  folder = state.config.last_folder || "";
  fillForm(state.config); envLine(); renderCards();
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
                self.title = "MI ↑"
                self.job_item.title = "Dry run in progress…" if job["dry_run"] else "Importing…"
            else:
                self.title = "MI"
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
