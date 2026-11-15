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
from http.server import ThreadingHTTPServer
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
FAKE_GO.write_text('#!/bin/bash\necho "args: $*"\necho "key=SECRET123"\n[[ "$*" == *--fail* ]] && exit 3\nexit 0\n')
FAKE_GO.chmod(0o755)


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
        self.assertEqual(app.scan_folder(str(CARD / "DCIM")), {"files": 2, "bytes": 3000})

    def test_volume_of(self):
        self.assertTrue(app.volume_of(str(CARD / "DCIM" / "100CANON")).endswith("EOS_DIGITAL"))
        self.assertIsNone(app.volume_of(TMP))

    def test_browse_lists_folders(self):
        listing = app.browse(str(CARD))
        self.assertEqual(listing["dirs"], ["DCIM", "MISC"])


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

    def test_hello_identifies_app(self):
        _, body = self.get("/api/hello")
        self.assertEqual(json.loads(body)["app"], app.APP_ID)


if __name__ == "__main__":
    unittest.main()
