"""Roadmap step 11 / ADR-010: `offload --proxy` queues ffmpeg-skill proxy on each verified video file while the next
files copy, links each proxy to its original by hash in the inventory, and never fails the offload on a bad proxy.

ffmpeg-skill is replaced by a stand-in folder with the same two entry points (scripts/proxy.py, scripts/_contract.py
doctor --json), so the suite needs no ffmpeg; with ffmpeg-skill and ffmpeg installed (INGEST_TEST_FFMPEG_SKILL_DIR),
one test runs the real one."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_skill import core  # noqa: E402
from ingest_skill.core import inventory, offload, verify  # noqa: E402
from test_cards import write  # noqa: E402
from test_fanout import OpenRecorder  # noqa: E402
from test_ingest import run_cli  # noqa: E402

FAKE_PROXY = r'''
import json, os, sys, time
args = sys.argv[1:]
src, out = args[0], args[args.index("-o") + 1]
with open(os.environ["FAKE_PROXY_LOG"], "a") as log:
    log.write(json.dumps({"input": src, "args": args, "started": time.time()}) + "\n")
time.sleep(float(os.environ.get("FAKE_PROXY_SECONDS", "0")))
if "BROKEN" in os.path.basename(src):
    print("error: Invalid data found when processing input", file=sys.stderr)
    sys.exit(1)
with open(src, "rb") as f, open(out, "wb") as o:
    o.write(b"PROXY of " + f.read(64))
print(json.dumps({"output": out, "hw": {"requested": "--hw" in args, "used": "--hw" in args}}))
'''
FAKE_CONTRACT = r'''
import json, os, sys
assert sys.argv[1:] == ["doctor", "--json"], sys.argv
print(json.dumps({"ok": True, "version": "3.1.8", "hw": {"platform_ok": os.environ.get("FAKE_HW") == "1", "default_on": False},
                  "gpu_encoders": {"h264_nvenc": "listed"}}))
'''
CLIPS = ["PRIVATE/M4ROOT/CLIP/C0001.MP4", "PRIVATE/M4ROOT/CLIP/C0002.MP4", "PRIVATE/M4ROOT/CLIP/C0003_BROKEN.MP4"]


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.skill = self.tmp / "ffmpeg-skill"
        write(self.skill / "scripts" / "proxy.py", FAKE_PROXY)
        write(self.skill / "scripts" / "_contract.py", FAKE_CONTRACT)
        self.log = self.tmp / "proxy-log.jsonl"
        self.card = self.tmp / "CARD_A"
        m4 = self.card / "PRIVATE" / "M4ROOT"
        write(m4 / "MEDIAPRO.XML", '<?xml version="1.0"?><MediaProfile xmlns="http://xmlns.sony.net/pro/metadata/mediaprofile"><Contents>'
              + "".join(f'<Material uri="./CLIP/{Path(c).name}" umid="0{i}"/>' for i, c in enumerate(CLIPS)) + "</Contents></MediaProfile>")
        write(self.card / CLIPS[0], os.urandom(600_000))
        write(self.card / CLIPS[1], os.urandom(400_000))
        write(self.card / CLIPS[2], os.urandom(1000))
        write(m4 / "CLIP" / "C0001M01.XML", '<?xml version="1.0"?><NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00"/>')
        write(self.card / "AUDIO" / "ZOOM0001.WAV", os.urandom(2000))   # audio: no proxy
        write(self.card / "RAW" / "A001_10300912_C001.braw", os.urandom(800))   # not decodable by ffmpeg: skipped, said so
        self.env = mock.patch.dict(os.environ, {"FAKE_PROXY_LOG": str(self.log)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_offload(self, *dests, **kw):
        return offload(str(self.card), [str(self.tmp / d) for d in dests], proxy=True, ffmpeg_skill=str(self.skill), **kw)

    def test_one_proxy_per_clip_linked_by_hash_and_a_failed_proxy_does_not_fail_the_offload(self):
        r = self.run_offload("ssd1", "ssd2")
        self.assertTrue(r.ok, "a failed proxy is not a failed offload")
        p = r.proxy
        self.assertEqual((p["made"], p["failed"], p["skipped"], p["exists"]), (2, 1, 1, 0))
        self.assertEqual((p["skill"], p["tool"], p["version"], p["encoder"]), ("ffmpeg-skill", "proxy", "3.1.8", "cpu"))
        self.assertEqual(p["gpu_encoders"], {"h264_nvenc": "listed"}, "listed, not used: no ffmpeg-skill tool takes NVENC")
        items = {i["original"]: i for i in p["items"]}
        self.assertEqual(items["RAW/A001_10300912_C001.braw"]["status"], "skipped")
        self.assertIn("Invalid data", items[CLIPS[2]]["detail"])
        self.assertTrue(any("proxy of " + CLIPS[2] in w for w in r.destinations[0].warnings))
        ssd1 = Path(r.destinations[0].root)
        self.assertEqual(sorted(c["input"] for c in self.calls()), sorted(str(ssd1 / c) for c in CLIPS), "made from the verified first copy, never the card")
        self.assertTrue(all("--hw" not in c["args"] for c in self.calls()))
        self.assertFalse(list((ssd1 / "ingest-proxies").rglob("*.ingest-part.mp4")), "no partial proxy left behind")
        self.assertFalse((Path(r.destinations[1].root) / "ingest-proxies").exists(), "one proxy per clip, not one per destination")
        index = json.loads((ssd1 / "ingest-proxies" / "proxies.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema"], "ingest/proxies@1")
        self.assertEqual([e["original"] for e in index["proxies"]], CLIPS[:2])
        for e in index["proxies"]:
            self.assertEqual(e["original_hash"], {"algorithm": "sha256", "value": hashlib.sha256((self.card / e["original"]).read_bytes()).hexdigest()})
            self.assertEqual(e["sha256"], hashlib.sha256((ssd1 / e["path"]).read_bytes()).hexdigest())
            self.assertEqual(e["made_by"]["tool"], "proxy")
        inv = inventory(str(ssd1))
        self.assertEqual(inv["clip_count"], 5, "the proxies are not clips of the card")
        links = {c["path"]: c["ingest_proxies"] for c in inv["clips"]}
        for rel in CLIPS[:2]:
            (link,) = links[rel]
            self.assertEqual((link["member"], link["path"], link["exists"]), (rel, f"ingest-proxies/{rel}.proxy.mp4", True))
            self.assertEqual(link["original_hash"]["value"], next(c for c in inv["clips"] if c["path"] == rel)["sha256"], "linked by the original's hash")
        self.assertEqual(links[CLIPS[2]], [])
        v = verify(str(ssd1))
        self.assertTrue(v["ok"])
        self.assertEqual(v["unlisted"], [], "the proxies folder is the copy's own, not unlisted media")
        # The second destination's inventory has the same clips and no proxy links.
        self.assertTrue(all(c["ingest_proxies"] == [] for c in inventory(r.destinations[1].root)["clips"]))

    def test_proxies_run_while_the_next_files_copy(self):
        os.environ["FAKE_PROXY_SECONDS"] = "0"
        seen = []
        with OpenRecorder() as rec, mock.patch.object(core, "CHUNK", 64 * 1024):
            # the second clip copies slowly (about 0.5 s), so the first clip's proxy has time to start meanwhile
            rec.faults.append((lambda p, mode: "w" in mode and "C0002" in p.name and p.name.endswith(".part"), {"write_delay": 0.08}))
            r = self.run_offload("ssd1", progress=lambda e: seen.append((e["event"], e.get("file"), time.time())))
        self.assertTrue(r.ok)
        first = next(c for c in self.calls() if c["input"].endswith("C0001.MP4"))
        c2_done = next(t for ev, f, t in seen if ev == "file" and f == CLIPS[1])
        self.assertLess(first["started"], c2_done, "the first clip's proxy started before the next clip finished copying")

    def test_hardware_encoder_when_ffmpeg_skill_reports_one(self):
        os.environ["FAKE_HW"] = "1"
        try:
            r = self.run_offload("ssd1")
        finally:
            del os.environ["FAKE_HW"]
        self.assertEqual(r.proxy["encoder"], "hw")
        self.assertTrue(all("--hw" in c["args"] for c in self.calls()))
        self.assertTrue(all(i["encoder"] == "hw" for i in r.proxy["items"] if i["status"] == "made"))

    def test_a_resume_keeps_the_proxies_already_made(self):
        self.run_offload("ssd1")
        n = len(self.calls())
        r = self.run_offload("ssd1")
        self.assertEqual((r.proxy["exists"], r.proxy["made"]), (2, 0))
        self.assertEqual(len(self.calls()), n + 1, "only the broken one is tried again")

    def test_missing_ffmpeg_skill_is_refused_before_anything_is_written(self):
        code, out = run_cli("offload", str(self.card), "--dest", str(self.tmp / "ssd1"), "--proxy", "--ffmpeg-skill", str(self.tmp / "nowhere"), "--json")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "PROXY_UNAVAILABLE"))
        self.assertFalse((self.tmp / "ssd1").exists())
        code, out = run_cli("offload", str(self.card), "--dest", str(self.tmp / "ssd1"), "--proxy", "--ffmpeg-skill", str(self.skill), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["proxy"]["made"], 2)

    def test_without_proxy_nothing_is_queued(self):
        r = offload(str(self.card), [str(self.tmp / "ssd1")])
        self.assertIsNone(r.proxy)
        self.assertNotIn("proxy", r.to_dict())
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(os.environ.get("INGEST_TEST_FFMPEG_SKILL_DIR") and shutil.which("ffmpeg"), "needs ffmpeg and INGEST_TEST_FFMPEG_SKILL_DIR")
    def test_real_ffmpeg_skill_proxy(self):
        card = self.tmp / "CAM"
        card.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25:d=1", "-c:v", "libx264", str(card / "C0001.MOV")], check=True)
        r = offload(str(card), [str(self.tmp / "real")], proxy=True, ffmpeg_skill=os.environ["INGEST_TEST_FFMPEG_SKILL_DIR"], proxy_width=160)
        self.assertEqual(r.proxy["made"], 1, r.proxy)
        (link,) = inventory(r.destinations[0].root)["clips"][0]["ingest_proxies"]
        self.assertTrue(link["exists"])


if __name__ == "__main__":
    unittest.main()
