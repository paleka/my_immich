"""Tests for my_immich. Run from the project folder:

    python3 -m unittest discover -s tests -v

They use a temporary folder for settings and a fake card, and never contact
Immich or run the real immich-go.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="my_immich_test_")
os.environ["MY_IMMICH_CONFIG_DIR"] = os.path.join(TMP, "config")
os.environ["MY_IMMICH_VOLUMES"] = os.path.join(TMP, "Volumes")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import my_immich as app  # noqa: E402

CARD = Path(TMP) / "Volumes" / "EOS_DIGITAL"
(CARD / "DCIM" / "100CANON").mkdir(parents=True)
(CARD / "DCIM" / "102WHALE").mkdir()
(CARD / "MISC").mkdir()
(Path(TMP) / "Volumes" / "Macintosh HD").mkdir()
(CARD / "DCIM" / "100CANON" / "IM5A0001.CR3").write_bytes(b"x" * 2000)
(CARD / "DCIM" / "102WHALE" / "IM5A0366.CR3").write_bytes(b"x" * 1000)
(CARD / "DCIM" / "100CANON" / "notes.txt").write_text("not media")
(CARD / "DCIM" / "100CANON" / ".hidden.jpg").write_bytes(b"x")

FAKE_GO = Path(TMP) / "immich-go"
FAKE_GO.write_text('#!/bin/bash\necho "args: $*"\necho "key=SECRET123"\n'
                   # the status line immich-go --no-ui rewrites in place with a carriage return
                   'printf "\\rImmich read 50%%, Assets found: 2, Upload errors: 0, Uploaded 0  "\n'
                   'echo; echo \'2026-01-01 10:00:00 INF uploaded successfully file="My Card:100CANON/IM5A0001.CR3"\'\n'
                   'printf "\\rImmich read 100%%, Assets found: 2, Upload errors: 1, Uploaded 1 ."\n'
                   'echo\n[[ "$*" == *--fail* ]] && exit 3\nexit 0\n')
FAKE_GO.chmod(0o755)


# A second stand-in that behaves like the real tool with a log file: per-file events go
# to --log-file, only the status line goes to the screen. With REFUSE in its name it
# rejects the flag, like a build that does not know it.
LOG_GO = Path(TMP) / "immich-go-logfile"
LOG_GO.write_text("""#!/bin/bash
log=/dev/null
for a in "$@"; do [[ "$a" == --log-file=* ]] && log="${a#--log-file=}"; done
if [[ "$0" == *REFUSE* && "$log" != /dev/null ]]; then echo "Error: unknown flag: --log-file"; exit 1; fi
echo '2026-01-01 10:00:00 INF discovered image file="DCIM:100CANON/IM5A0001.CR3"' >> "$log"
echo '2026-01-01 10:00:00 INF discovered image file="DCIM:102WHALE/IM5A0366.CR3"' >> "$log"
echo '2026-01-01 10:00:01 INF uploaded successfully file="DCIM:100CANON/IM5A0001.CR3"' >> "$log"
echo '2026-01-01 10:00:01 INF tagged file="DCIM:100CANON/IM5A0001.CR3" tag=x' >> "$log"
echo '2026-01-01 10:00:02 INF server has duplicate file="DCIM:102WHALE/IM5A0366.CR3"' >> "$log"
printf "\\rImmich read 100%%, Assets found: 2, Upload errors: 0, Uploaded 1  \\n"
""")
LOG_GO.chmod(0o755)
REFUSE_GO = Path(TMP) / "immich-go-REFUSE"
REFUSE_GO.write_text(LOG_GO.read_text())
REFUSE_GO.chmod(0o755)


def cfg(**over):
    data = dict(app.DEFAULTS, server="http://immich.local:2283", api_key="SECRET123",
                immich_go_path=str(FAKE_GO), keep_awake=False)
    data.update(over)
    return app.clean_config(data)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_cleaning(self):
        c = app.clean_config({"server": "http://x:1/ ", "raw_jpeg": "Nonsense", "session_tag": 0})
        self.assertEqual(c["server"], "http://x:1")
        self.assertEqual(c["raw_jpeg"], app.DEFAULTS["raw_jpeg"])   # bad enum ignored
        self.assertFalse(c["session_tag"])

    def test_recent_folders_are_kept_and_capped(self):
        c = app.clean_config({"recent_folders": ["/a", "/b", 3, ""] + ["/x%d" % i for i in range(10)]})
        self.assertEqual(c["recent_folders"][:2], ["/a", "/b"])
        self.assertEqual(len(c["recent_folders"]), app.MAX_RECENT)
        self.assertEqual(app.clean_config({})["recent_folders"], [])

    def test_on_errors_accepts_a_count(self):
        self.assertEqual(app.clean_config({"on_errors": "5"})["on_errors"], "5")
        self.assertEqual(app.clean_config({"on_errors": "whatever"})["on_errors"], "stop")

    def test_save_is_private_and_round_trips(self):
        app.save_config(cfg(burst="Stack"))
        mode = stat.S_IMODE(os.stat(app.CONFIG_FILE).st_mode)
        self.assertEqual(mode, 0o600)
        loaded, imported = app.load_config()
        self.assertEqual(loaded["burst"], "Stack")
        self.assertIsNone(imported)

    def test_import_from_old_script(self):
        script = Path(TMP) / "sd-to-immich.sh"
        script.write_text('IMMICH_SERVER="${IMMICH_SERVER:-http://192.168.1.5:2283}"\n'
                          'RAW_JPEG_MODE="${RAW_JPEG_MODE:-StackCoverRaw}"   # comment\n'
                          'SESSION_TAG="${SESSION_TAG:-false}"\n'
                          'API_KEY="abc123"\n')
        old = app.OLD_SCRIPT_CANDIDATES
        app.OLD_SCRIPT_CANDIDATES = [script]
        try:
            found = app.import_old_script()
        finally:
            app.OLD_SCRIPT_CANDIDATES = old
        self.assertEqual(found["server"], "http://192.168.1.5:2283")
        self.assertEqual(found["api_key"], "abc123")
        self.assertEqual(found["raw_jpeg"], "StackCoverRaw")
        self.assertFalse(found["session_tag"])


class CommandTests(unittest.TestCase):
    def test_default_command(self):
        cmd = app.build_command(cfg(), "/src", False, "immich-go")
        self.assertEqual(cmd[:3], ["immich-go", "upload", "from-folder"])
        self.assertIn("--no-ui", cmd)
        self.assertIn("--manage-raw-jpeg=KeepRaw", cmd)
        self.assertIn("--session-tag", cmd)
        self.assertNotIn("--dry-run", cmd)
        self.assertEqual(cmd[-1], "/src")

    def test_options_map_to_flags(self):
        cmd = app.build_command(cfg(raw_jpeg="NoStack", burst="Stack", session_tag=False, pause_jobs=False,
                                    folder_as_album="FOLDER", into_album="Trip", include_type="IMAGE",
                                    recursive=False, extra_args='--tag "a b"'), "/src", True, "immich-go")
        self.assertFalse(any(c.startswith("--manage-raw-jpeg") for c in cmd))
        for flag in ("--manage-burst=Stack", "--pause-immich-jobs=false", "--folder-as-album=FOLDER",
                     "--into-album=Trip", "--include-type=IMAGE", "--recursive=false", "--dry-run"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--session-tag", cmd)
        self.assertEqual(cmd[-3:], ["--tag", "a b", "/src"])

    def test_mask_hides_key(self):
        self.assertNotIn("SECRET123", app.mask("--api-key=SECRET123", "SECRET123"))


class SourceTests(unittest.TestCase):
    def test_card_detection(self):
        cards = app.list_cards()
        self.assertEqual([c["name"] for c in cards], ["EOS_DIGITAL"])
        self.assertTrue(cards[0]["path"].endswith("EOS_DIGITAL/DCIM"))
        self.assertEqual(cards[0]["folders"], ["100CANON", "102WHALE"])

    def test_scan_counts_media_only(self):
        found = app.scan_folder(str(CARD / "DCIM"))
        self.assertEqual((found["files"], found["bytes"]), (2, 3000))
        self.assertEqual((found["photos"], found["videos"], found["raw"]), (2, 0, 2))
        self.assertLessEqual(found["oldest"], found["newest"])

    def test_scan_samples_are_photos(self):
        found = app.scan_folder(str(CARD / "DCIM"), want_samples=True)
        self.assertEqual([os.path.basename(p) for p in found["samples"]], ["IM5A0001.CR3", "IM5A0366.CR3"])

    def test_thumbnail_rules(self):
        self.assertIsNone(app.make_thumb(str(CARD / "DCIM" / "100CANON" / "notes.txt")))   # not media
        self.assertIsNone(app.make_thumb("/no/such/file.jpg"))

    def test_volume_of(self):
        self.assertTrue(app.volume_of(str(CARD / "DCIM" / "100CANON")).endswith("EOS_DIGITAL"))
        self.assertIsNone(app.volume_of(TMP))

    def test_browse_lists_folders(self):
        listing = app.browse(str(CARD))
        self.assertEqual(listing["dirs"], ["DCIM", "MISC"])
        self.assertIsNone(listing["error"])
        self.assertEqual(listing["crumbs"][-1]["label"], "EOS_DIGITAL")
        self.assertEqual(listing["crumbs"][-1]["path"], listing["path"])

    def test_browse_reports_missing_folder(self):
        listing = app.browse("/definitely/not/here")
        self.assertIn("does not exist", listing["error"])

    def test_browse_counts_media_in_folder(self):
        self.assertEqual(app.browse(str(CARD / "DCIM" / "100CANON"))["media_here"], 1)
        self.assertEqual(app.browse(str(CARD / "DCIM"))["media_here"], 0)

    def test_drives_exclude_cards(self):
        names = [d["name"] for d in app.list_drives()]
        self.assertIn("Macintosh HD", names)       # a plain folder here; the real startup disk is skipped
        self.assertNotIn("EOS_DIGITAL", names)

    def test_places_include_card_and_drive(self):
        kinds = {p["label"]: p["kind"] for p in app.places()}
        self.assertEqual(kinds["EOS_DIGITAL"], "card")
        self.assertEqual(kinds["Macintosh HD"], "drive")


class JobTests(unittest.TestCase):
    def run_job(self, config, source, dry_run=True):
        job = app.Job()
        error = job.start(config, source, dry_run)
        if error:
            return job, error
        for _ in range(100):
            if not job.snapshot(0)["running"]:
                break
            time.sleep(0.05)
        return job, None

    def test_successful_run_masks_key(self):
        job, error = self.run_job(cfg(), str(CARD / "DCIM"))
        self.assertIsNone(error)
        snap = job.snapshot(0)
        self.assertEqual(snap["returncode"], 0)
        text = "\n".join(snap["lines"])
        self.assertIn("--dry-run", text)
        self.assertNotIn("SECRET123", text)

    def test_status_line_becomes_progress_not_log(self):
        job, _ = self.run_job(cfg(), str(CARD / "DCIM"))
        snap = job.snapshot(0)
        self.assertEqual(snap["progress"], {"immich_read": 100, "found": 2, "errors": 1, "uploaded": 1})
        status_lines = [line for line in snap["lines"] if "Assets found" in line]
        self.assertEqual(len(status_lines), 1)                     # only the final one is kept
        self.assertEqual(snap["current"], "100CANON/IM5A0001.CR3")  # quoted name with a space in the source

    def test_source_is_counted_for_the_progress_panel(self):
        job, _ = self.run_job(cfg(), str(CARD / "DCIM"))
        for _ in range(100):
            if job.snapshot(0)["totals"]:
                break
            time.sleep(0.02)
        snap = job.snapshot(0)
        self.assertEqual(snap["totals"]["files"], 2)
        self.assertEqual(len(snap["samples"]), 2)

    def test_events_from_the_log_file_are_counted(self):
        job, error = self.run_job(cfg(immich_go_path=str(LOG_GO)), str(CARD / "DCIM"))
        self.assertIsNone(error)
        snap = job.snapshot(0)
        self.assertEqual(snap["counters"], {"found_images": 2, "uploaded": 1, "tagged": 1, "on_server": 1})
        self.assertEqual(snap["bytes_up"], 2000)                    # size of the one uploaded file
        self.assertEqual([f["kind"] for f in snap["feed"]], ["uploaded", "on_server"])
        self.assertTrue(any("uploaded successfully" in line for line in snap["lines"]))   # shown in the log
        self.assertTrue(any("--log-file=" in line for line in snap["lines"][:1]))

    def test_runs_again_without_log_file_when_refused(self):
        job, _ = self.run_job(cfg(immich_go_path=str(REFUSE_GO)), str(CARD / "DCIM"))
        snap = job.snapshot(0)
        self.assertEqual(snap["returncode"], 0)
        self.assertTrue(any("running again without it" in line for line in snap["lines"]))
        self.assertEqual(snap["progress"]["uploaded"], 1)

    def test_own_log_file_setting_is_respected(self):
        job, _ = self.run_job(cfg(immich_go_path=str(LOG_GO), extra_args="--log-file=/dev/null"), str(CARD / "DCIM"))
        commands = [line for line in job.snapshot(0)["lines"] if line.startswith("$ ")]
        self.assertEqual(commands[0].count("--log-file"), 1)

    def test_network_counters_are_readable_or_absent(self):
        counters = app.net_counters()
        self.assertTrue(counters is None or (len(counters) == 2 and all(c >= 0 for c in counters)))

    def test_failure_exit_code(self):
        job, _ = self.run_job(cfg(extra_args="--fail"), str(CARD / "DCIM"))
        self.assertEqual(job.snapshot(0)["returncode"], 3)

    def test_prechecks(self):
        _, error = self.run_job(cfg(), "/does/not/exist")
        self.assertIn("does not exist", error)
        _, error = self.run_job(cfg(api_key=""), str(CARD / "DCIM"))
        self.assertIn("API key", error)
        _, error = self.run_job(cfg(immich_go_path="/nope/immich-go"), str(CARD / "DCIM"))
        self.assertIn("immich-go was not found", error)


class FakeImmich(BaseHTTPRequestHandler):
    """Stands in for Immich behind a proxy that, like Cloudflare, refuses
    Python's default user agent before the request reaches the server."""
    blocked = False  # when True, refuse everything, as an access gateway would

    def log_message(self, *args):
        pass

    def do_GET(self):
        ua = self.headers.get("User-Agent", "")
        if self.blocked or not ua or ua.startswith("Python-urllib"):
            return self.reply(403, b"error code: 1010", "text/plain")
        if self.path == "/api/server/ping":
            return self.reply(200, b'{"res": "pong"}')
        if self.path == "/api/users/me":
            if self.headers.get("x-api-key") == "good-key":
                return self.reply(200, b'{"name": "Demo User"}')
            return self.reply(401, b'{"message": "Invalid API key"}')
        self.reply(404, b"{}")

    def reply(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeImmich)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def tearDown(self):
        FakeImmich.blocked = False

    def test_connection_gets_past_a_proxy_that_blocks_python_urllib(self):
        result = app.test_connection(cfg(server=self.base, api_key="good-key"))
        self.assertTrue(result["ok"], result["message"])
        self.assertIn("Demo User", result["message"])

    def test_server_stats_send_the_user_agent_too(self):
        self.assertIn("ping_ms", app.server_stats(cfg(server=self.base, api_key="good-key")))

    def test_http_403_is_not_reported_as_a_network_problem(self):
        FakeImmich.blocked = True
        result = app.test_connection(cfg(server=self.base, api_key="good-key"))
        self.assertFalse(result["ok"])
        self.assertIn("HTTP 403", result["message"])
        self.assertIn("error code: 1010", result["message"])
        self.assertNotIn("same network", result["message"])

    def test_refused_key_reports_immichs_message(self):
        result = app.test_connection(cfg(server=self.base, api_key="bad-key"))
        self.assertFalse(result["ok"])
        self.assertIn("API key was refused (HTTP 401)", result["message"])
        self.assertIn("Invalid API key", result["message"])
        self.assertNotIn("bad-key", result["message"])

    def test_unreachable_server_still_says_cannot_reach(self):
        result = app.test_connection(cfg(server="http://127.0.0.1:1", api_key="good-key"))
        self.assertIn("Cannot reach Immich", result["message"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.STATE["config"] = cfg()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()

    def test_page_serves_and_carries_token(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(app.TOKEN, body)

    def test_api_needs_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/state")
        self.assertEqual(ctx.exception.code, 403)

    def test_api_rejects_foreign_host(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/state", {"X-Token": app.TOKEN, "Host": "evil.example"})
        self.assertEqual(ctx.exception.code, 403)

    def test_state_with_token(self):
        _, body = self.get("/api/state", {"X-Token": app.TOKEN})
        data = json.loads(body)
        self.assertEqual(data["cards"][0]["name"], "EOS_DIGITAL")
        self.assertEqual(data["config"]["server"], "http://immich.local:2283")

    def test_thumbnail_needs_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/thumb?path=/etc/hosts")
        self.assertEqual(ctx.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/thumb?t=%s&path=/etc/hosts" % app.TOKEN)   # right token, but not a photo
        self.assertEqual(ctx.exception.code, 404)

    def test_hello_identifies_app(self):
        _, body = self.get("/api/hello")
        self.assertEqual(json.loads(body)["app"], app.APP_ID)


if __name__ == "__main__":
    unittest.main()
