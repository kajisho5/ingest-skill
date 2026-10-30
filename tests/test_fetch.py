"""ingest fetch (ECOSYSTEM_ROADMAP.md step 7, ADR-005): a URL downloaded with yt-dlp lands like a card -- verified
copies, ingest-manifest.json with the redacted download record, ASC MHL history, inventory, and `verify` passes.

No test reaches the internet. yt-dlp is replaced by a fake module (FakeYtDlp) that writes a small local file, so these
run without the extra. With the extra installed, one test downloads from a local HTTP server on 127.0.0.1 through the
real yt-dlp (the roadmap's "fixture URL"). INGEST_FETCH_NETWORK_URL=<url> runs one real download from the internet;
it is skipped by default."""
from __future__ import annotations

import functools
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest_skill import fetch as fetch_mod  # noqa: E402
from ingest_skill.cli import main  # noqa: E402
from ingest_skill.core import MANIFEST_NAME, IngestError, doctor, inventory, sha256_file, verify  # noqa: E402
from ingest_skill.fetch import fetch, redact_text, redact_url  # noqa: E402

SECRETS = ("s3cr3t-token", "SIGVALUE", "hunter2", "COOKIEVALUE", "cdn-signature", "AKIAEXAMPLE", "frag-token", "JSESSION")
URL = "https://alice:hunter2@video.example.com/watch?v=abc123&token=s3cr3t-token&t=30&Signature=SIGVALUE#access_token=frag-token"


class FakeYtDlp:
    """Just enough of yt_dlp's embedding API (YoutubeDL(params) as a context manager, extract_info(url, download)) to
    run fetch without the network: download=True writes a small file at the outtmpl. Every call is recorded."""

    def __init__(self, mode: str = "ok", version: str = "2026.08.19", payload: bytes = b"FAKE-MP4-" + bytes(range(256)) * 64):
        self.mode, self.payload, self.calls = mode, payload, []
        self.module = types.ModuleType("yt_dlp")
        self.module.YoutubeDL = self._factory
        self.version = types.ModuleType("yt_dlp.version")
        self.version.__version__ = version
        self.module.version = self.version

    def _factory(self, params):
        fake = self

        class YoutubeDL:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                fake.calls.append({"url": url, "download": download, "params": dict(params)})
                if fake.mode == "fail":
                    raise RuntimeError(f"ERROR: [generic] Unable to download webpage {url}: HTTP Error 403: Forbidden")
                info = {"id": "abc123", "title": "フェイク session", "extractor": "fakesite", "extractor_key": "FakeSite",
                        "webpage_url": url, "duration": 42.5, "is_live": fake.mode == "live", "was_live": fake.mode == "was_live",
                        # What must never be recorded: the signed media URL and the request headers (with a cookie).
                        "url": "https://cdn.example.com/v.mp4?sig=cdn-signature&X-Amz-Credential=AKIAEXAMPLE",
                        "http_headers": {"Cookie": "SID=COOKIEVALUE", "User-Agent": "x"}}
                if fake.mode == "playlist":
                    return {"_type": "playlist", "id": "PL1", "entries": [info]}
                if not download:
                    return info
                info["format_id"] = "137+140"
                if fake.mode == "no_file":
                    return info
                out = Path(params["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", "mp4"))
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(fake.payload)
                if fake.mode == "partial":
                    out.with_name(out.name + ".part").write_bytes(b"half")
                info["requested_downloads"] = [{"filepath": str(out)}]
                return info

        return YoutubeDL()

    @contextmanager
    def installed(self):
        with mock.patch.dict(sys.modules, {"yt_dlp": self.module, "yt_dlp.version": self.version}):
            yield self


@contextmanager
def no_yt_dlp():
    """As if the extra were not installed: importing yt_dlp raises ImportError."""
    with mock.patch.dict(sys.modules, {"yt_dlp": None, "yt_dlp.version": None}):
        yield


def run_cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(list(args))
    return code, buf.getvalue()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.staging = self.tmp / "staging"
        self.dests = [str(self.tmp / "ssd1"), str(self.tmp / "ssd2")]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_nothing_written(self):
        for d in self.dests:
            self.assertFalse(Path(d).exists(), f"{d} was written")
        self.assertEqual(list(self.staging.iterdir()) if self.staging.exists() else [], [], "staging left behind")

    def test_success_lands_like_a_card_with_a_redacted_record(self):
        fake = FakeYtDlp()
        with fake.installed():
            d = fetch(URL, self.dests, project="2026-10-30_event", hash_algorithm="sha256", format_selector="bestvideo*+bestaudio/best",
                      staging=str(self.staging))
        self.assertTrue(d["ok"], d["destinations"])
        self.assertEqual(d["schema"], "ingest/fetch-report@1")
        # yt-dlp got the real URL, as one Python value (no command line), with no cookies and no playlist.
        self.assertEqual([c["url"] for c in fake.calls], [URL, URL])
        self.assertEqual([c["download"] for c in fake.calls], [False, True])
        for c in fake.calls:
            p = c["params"]
            self.assertTrue(p["noplaylist"])
            self.assertIsNone(p["cookiefile"])
            self.assertIsNone(p["cookiesfrombrowser"])
            self.assertFalse(p["allow_unplayable_formats"])
            self.assertNotIn("live_from_start", p)
            self.assertEqual(p["format"], "bestvideo*+bestaudio/best")
        f = d["fetch"]
        self.assertEqual(f["source_url"], "https://video.example.com/watch?v=abc123&token=REDACTED&t=30&Signature=REDACTED#access_token=REDACTED")
        self.assertEqual(f["webpage_url"], f["source_url"])
        self.assertEqual(f["redacted"], ["#access_token", "Signature", "token", "userinfo"])
        self.assertEqual((f["extractor"], f["extractor_key"], f["format_id"], f["id"]), ("fakesite", "FakeSite", "137+140", "abc123"))
        self.assertEqual(f["downloader"], {"name": "yt-dlp", "version": "2026.08.19"})
        self.assertEqual((f["format_selector"], f["files"], f["is_live"], f["live_from_start"]), ("bestvideo*+bestaudio/best", ["abc123.mp4"], False, False))
        self.assertTrue(f["started_at"] <= f["fetched_at"])
        self.assertEqual(d["staging"]["kept"], False)
        self.assertEqual(list(self.staging.iterdir()), [], "staging is removed once every destination is verified")
        for dest in d["destinations"]:
            root = Path(dest.get("root"))
            self.assertEqual(root.parts[-2:], ("2026-10-30_event", "fakesite-abc123"))
            self.assertEqual((dest["copied"], dest["failed"]), (1, 0))
            self.assertTrue(dest["mhl"], "an ASC MHL generation, as for a card")
            self.assertEqual((root / "abc123.mp4").read_bytes(), fake.payload)
            manifest_text = (root / MANIFEST_NAME).read_text(encoding="utf-8")
            for secret in SECRETS:
                self.assertNotIn(secret, manifest_text)
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["fetch"], f)
            self.assertEqual(manifest["files"][0]["sha256"], sha256_file(root / "abc123.mp4"))
            v = verify(str(root))
            self.assertTrue(v["ok"], v["failures"])
            self.assertEqual(v["ok_files"], 1)
            inv = inventory(str(root))
            self.assertEqual(inv["fetch"], f)
            self.assertEqual([c["path"] for c in inv["clips"]], ["abc123.mp4"])
            self.assertTrue(inv["clips"][0]["sha256"])
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(d, ensure_ascii=False))
        # verify catches damage on fetched material exactly as on a card.
        root = Path(d["destinations"][0]["root"])
        data = bytearray((root / "abc123.mp4").read_bytes())
        data[100] ^= 1
        (root / "abc123.mp4").write_bytes(bytes(data))
        v = verify(str(root))
        self.assertFalse(v["ok"])
        self.assertEqual({x["code"] for x in v["failures"]}, {"VERIFY_MISMATCH"})

    def test_cli_json_and_default_name(self):
        with FakeYtDlp().installed():
            code, out = run_cli("fetch", "https://video.example.com/watch?v=abc123", "--dest", self.dests[0], "--staging", str(self.staging), "--json")
        self.assertEqual(code, 0, out)
        d = json.loads(out)
        self.assertTrue(d["ok"])
        self.assertEqual(Path(d["destinations"][0]["root"]).name, "fakesite-abc123")
        self.assertEqual(d["fetch"]["redacted"], [])
        self.assertEqual(d["fetch"]["format_selector"], fetch_mod.FORMAT_SELECTOR if shutil.which("ffmpeg") else fetch_mod.FORMAT_SELECTOR_NO_FFMPEG)
        with FakeYtDlp().installed():
            code, out = run_cli("fetch", "https://video.example.com/watch?v=abc123", "--dest", self.dests[1], "--name", "B007",
                                "--staging", str(self.staging), "--hash", "sha256")
        self.assertEqual(code, 0, out)
        self.assertTrue((Path(self.dests[1]) / "B007" / "abc123.mp4").is_file())

    def test_live_stream_is_captured_from_its_start(self):
        fake = FakeYtDlp("live")
        with fake.installed():
            d = fetch("https://video.example.com/live", self.dests[:1], staging=str(self.staging))
        self.assertTrue(d["ok"])
        self.assertNotIn("live_from_start", fake.calls[0]["params"])
        self.assertTrue(fake.calls[1]["params"]["live_from_start"])
        self.assertEqual((d["fetch"]["is_live"], d["fetch"]["live_from_start"]), (True, True))
        fake = FakeYtDlp("was_live")
        with fake.installed():
            d = fetch("https://video.example.com/ended", [str(self.tmp / "ssd3")], staging=str(self.staging))
        self.assertNotIn("live_from_start", fake.calls[1]["params"], "an ended stream downloads like any video")
        self.assertEqual((d["fetch"]["was_live"], d["fetch"]["live_from_start"]), (True, False))

    def test_without_yt_dlp_it_is_refused_before_anything_is_written(self):
        with no_yt_dlp():
            with self.assertRaises(IngestError) as cm:
                fetch(URL, self.dests, staging=str(self.staging))
            self.assertEqual(cm.exception.code, "FETCH_UNAVAILABLE")
            self.assertIn("ingest-skill[fetch]", str(cm.exception))
            self.assert_nothing_written()
            code, out = run_cli("fetch", URL, "--dest", self.dests[0], "--staging", str(self.staging), "--json")
            self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "FETCH_UNAVAILABLE"))
            self.assert_nothing_written()
            doc = doctor()
            self.assertTrue(doc["ok"], "a missing optional extra is not a failure")
            self.assertEqual((doc["checks"]["yt-dlp"]["status"], doc["checks"]["yt-dlp"]["version"]), ("unavailable", None))
            self.assertIn("ingest-skill[fetch]", doc["checks"]["yt-dlp"]["detail"])
        with FakeYtDlp(version="2026.09.01").installed():
            check = doctor()["checks"]["yt-dlp"]
        self.assertEqual((check["status"], check["version"]), ("ok", "2026.09.01"))

    def test_refusals_happen_before_the_download(self):
        fake = FakeYtDlp()
        cases = [("ftp://example.com/a.mp4", self.dests, None, "URL_INVALID"), ("file:///etc/passwd", self.dests, None, "URL_INVALID"),
                 ("-o/tmp/x https://example.com", self.dests, None, "URL_INVALID"), ("https://", self.dests, None, "URL_INVALID"),
                 (URL, [], None, "NO_DESTINATION"), (URL, [self.dests[0], self.dests[0]], None, "DUPLICATE_DESTINATION"),
                 (URL, self.dests, "../escape", "NAME_INVALID")]
        with fake.installed():
            for url, dests, name, code in cases:
                with self.subTest(code=code, url=url):
                    with self.assertRaises(IngestError) as cm:
                        fetch(url, dests, name=name, staging=str(self.staging))
                    self.assertEqual(cm.exception.code, code)
                    for secret in SECRETS:
                        self.assertNotIn(secret, str(cm.exception))
            # A destination inside the staging folder's parent is fine; one inside the staging folder itself is not
            # possible to name in advance, and one around it is refused.
            with self.assertRaises(IngestError) as cm:
                fetch(URL, [str(self.tmp)], staging=str(self.staging))
            self.assertEqual(cm.exception.code, "DESTINATION_OVERLAPS_SOURCE")
        self.assertEqual(fake.calls, [], "nothing was downloaded")
        self.assertFalse(Path(self.dests[0]).exists())
        self.assertEqual(list(self.staging.iterdir()), [])
        code, out = run_cli("fetch", "ftp://example.com/a.mp4", "--dest", self.dests[0], "--json")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "URL_INVALID"))

    def test_a_failed_download_writes_nothing_and_its_message_is_redacted(self):
        for mode, needle in (("fail", "403"), ("playlist", "playlist"), ("no_file", "no file"), ("partial", "incomplete")):
            with self.subTest(mode=mode), FakeYtDlp(mode).installed():
                with self.assertRaises(IngestError) as cm:
                    fetch(URL, self.dests, staging=str(self.staging))
                self.assertEqual(cm.exception.code, "FETCH_FAILED")
                self.assertIn(needle, str(cm.exception))
                for secret in SECRETS:
                    self.assertNotIn(secret, str(cm.exception))
                self.assert_nothing_written()
        with FakeYtDlp("fail").installed():
            code, out = run_cli("fetch", URL, "--dest", self.dests[0], "--staging", str(self.staging), "--json")
        err = json.loads(out)["error"]
        self.assertEqual((code, err["code"]), (3, "FETCH_FAILED"))
        self.assertIn("https://video.example.com/watch?v=abc123&token=REDACTED", err["message"])

    def test_staging_is_kept_when_a_destination_is_not_verified(self):
        clash = Path(self.dests[1]) / "fakesite-abc123" / "abc123.mp4"
        clash.parent.mkdir(parents=True)
        clash.write_bytes(b"someone else's file")
        with FakeYtDlp().installed():
            d = fetch(URL, self.dests, staging=str(self.staging))
        self.assertFalse(d["ok"])
        self.assertEqual([x["conflicts"] for x in d["destinations"]], [0, 1])
        self.assertEqual(clash.read_bytes(), b"someone else's file", "never overwritten")
        self.assertTrue(d["staging"]["kept"])
        self.assertTrue((Path(d["staging"]["path"]) / "fakesite-abc123" / "abc123.mp4").is_file(), "the download is not lost")
        self.assertTrue(verify(d["destinations"][0]["root"])["ok"], "the other destination is complete")


class RedactionTests(unittest.TestCase):
    def test_credentials_are_removed_and_the_rest_kept(self):
        cases = [
            (URL, "https://video.example.com/watch?v=abc123&token=REDACTED&t=30&Signature=REDACTED#access_token=REDACTED",
             ["userinfo", "token", "Signature", "#access_token"]),
            ("https://cdn.example.com/a.mp4?X-Amz-Algorithm=AWS4&X-Amz-Credential=AKIAEXAMPLE&X-Amz-Signature=abc&X-Amz-Security-Token=t",
             "https://cdn.example.com/a.mp4?X-Amz-Algorithm=AWS4&X-Amz-Credential=REDACTED&X-Amz-Signature=REDACTED&X-Amz-Security-Token=REDACTED",
             ["X-Amz-Credential", "X-Amz-Signature", "X-Amz-Security-Token"]),
            ("https://e.com/v?api_key=1&apikey=2&key=3&sig=4&auth=5&password=6&pwd=7&session_id=8&jwt=9&hdnts=10&lang=ja",
             "https://e.com/v?api_key=REDACTED&apikey=REDACTED&key=REDACTED&sig=REDACTED&auth=REDACTED&password=REDACTED&pwd=REDACTED"
             "&session_id=REDACTED&jwt=REDACTED&hdnts=REDACTED&lang=ja",
             ["api_key", "apikey", "key", "sig", "auth", "password", "pwd", "session_id", "jwt", "hdnts"]),
            ("https://e.com/app;jsessionid=JSESSION/watch?v=1", "https://e.com/app;jsessionid=REDACTED/watch?v=1", [";jsessionid"]),
            ("https://bob@e.com:8443/v?v=1#t=30", "https://e.com:8443/v?v=1#t=30", ["userinfo"]),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42", []),
            ("https://e.com/v?token=&x=1", "https://e.com/v?token=&x=1", []),
        ]
        for url, want, removed in cases:
            with self.subTest(url=url):
                self.assertEqual(redact_url(url), (want, removed))

    def test_urls_inside_messages_are_redacted(self):
        msg = f"ERROR: unable to download {URL} (see https://e.com/help?x=1)"
        self.assertEqual(redact_text(msg), "ERROR: unable to download https://video.example.com/watch?v=abc123&token=REDACTED&t=30"
                                           "&Signature=REDACTED#access_token=REDACTED (see https://e.com/help?x=1)")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass   # yt-dlp may close a connection once it has the headers: a broken pipe here is expected


@unittest.skipIf(fetch_mod.load_yt_dlp() is None, "yt-dlp extra not installed (pip install -e '.[fetch]')")
class RealYtDlpTests(unittest.TestCase):
    """The real yt-dlp library, against a fixture file served on 127.0.0.1 (no internet): its generic extractor
    downloads a direct media link."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.www = self.tmp / "www"
        self.www.mkdir()
        self.payload = b"\x00\x00\x00\x18ftypmp42" + os.urandom(64 * 1024)
        (self.www / "clip.mp4").write_bytes(self.payload)
        handler = functools.partial(_QuietHandler, directory=str(self.www))
        self.server = _QuietServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixture_url_lands_with_a_manifest_and_verifies(self):
        port = self.server.server_address[1]
        url = f"http://user:pa55@127.0.0.1:{port}/clip.mp4?token=s3cr3t-token"
        d = fetch(url, [str(self.tmp / "ssd1"), str(self.tmp / "ssd2")], project="fixture", format_selector="best", staging=str(self.tmp / "staging"))
        self.assertTrue(d["ok"], d)
        f = d["fetch"]
        self.assertEqual(f["source_url"], f"http://127.0.0.1:{port}/clip.mp4?token=REDACTED")
        self.assertEqual((f["extractor"], f["redacted"]), ("generic", ["token", "userinfo"]))
        self.assertEqual(f["downloader"]["version"], fetch_mod.yt_dlp_version(fetch_mod.load_yt_dlp()))
        self.assertTrue(f["format_id"])
        for dest in d["destinations"]:
            root = Path(dest["root"])
            self.assertEqual([p.name for p in root.iterdir() if p.is_file() and p.name != MANIFEST_NAME], ["clip.mp4"])
            self.assertEqual((root / "clip.mp4").read_bytes(), self.payload)
            text = (root / MANIFEST_NAME).read_text(encoding="utf-8")
            self.assertNotIn("s3cr3t-token", text)
            self.assertNotIn("pa55", text)
            self.assertEqual(json.loads(text)["fetch"], f)
            self.assertTrue(verify(str(root))["ok"])

    def test_a_missing_page_is_fetch_failed_and_writes_nothing(self):
        url = f"http://127.0.0.1:{self.server.server_address[1]}/missing.mp4?sig=SIGVALUE"
        with self.assertRaises(IngestError) as cm:
            fetch(url, [str(self.tmp / "ssd1")], format_selector="best", staging=str(self.tmp / "staging"))
        self.assertEqual(cm.exception.code, "FETCH_FAILED")
        self.assertNotIn("SIGVALUE", str(cm.exception))
        self.assertFalse((self.tmp / "ssd1").exists())


@unittest.skipUnless(os.environ.get("INGEST_FETCH_NETWORK_URL"), "set INGEST_FETCH_NETWORK_URL=<a video URL you may download> to run a real download")
class NetworkFetchTests(unittest.TestCase):
    def test_a_real_url(self):
        if fetch_mod.load_yt_dlp() is None:
            self.fail("INGEST_FETCH_NETWORK_URL is set but yt-dlp is not installed")
        tmp = Path(tempfile.mkdtemp()).resolve()
        try:
            d = fetch(os.environ["INGEST_FETCH_NETWORK_URL"], [str(tmp / "ssd1")], staging=str(tmp / "staging"))
            self.assertTrue(d["ok"], d)
            self.assertTrue(verify(d["destinations"][0]["root"])["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
