"""Roadmap step 8 / ADR-007: `offload --follow` copies recordings still being written as they grow (appended bytes
only), lists them as growing partial copies, and seals each one (manifest "files", ASC MHL) only once it is stable.

The recorder is simulated: the test's sleep() appends a burst to the source file between polls, and its clock()
advances by the poll interval, so no test waits for a real settle window."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_skill import core  # noqa: E402
from ingest_skill.core import MANIFEST_NAME, inventory, offload, verify  # noqa: E402
from test_fanout import OpenRecorder  # noqa: E402
from test_ingest import run_cli  # noqa: E402

ASCMHL = "urn:ASC:MHL:v2.0"
REL = "CLIPS/REC_0001.MOV"
SMALL_CHUNK = 64 * 1024


class Recorder:
    """A fake clock plus a sleep() that, on its first len(bursts) calls, appends the next burst to the file being
    recorded (or runs a callable), then only lets time pass."""

    def __init__(self, path: Path, bursts):
        self.path, self.bursts, self.t, self.sleeps = path, list(bursts), 1000.0, 0

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        if self.sleeps < len(self.bursts):
            b = self.bursts[self.sleeps]
            if callable(b):
                b()
            else:
                with open(self.path, "ab") as f:
                    f.write(b)
        self.sleeps += 1


def mhl_records(root: Path, rel: str):
    """Every hash entry for rel in every ASC MHL generation of root."""
    out = []
    for gen in sorted((root / "ascmhl").glob("*.mhl")):
        for h in ET.parse(gen).getroot().iter(f"{{{ASCMHL}}}hash"):
            if h.find(f"{{{ASCMHL}}}path").text == rel:
                out.append(gen.name)
    return out


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.card = self.tmp / "RECORDER"
        (self.card / "CLIPS").mkdir(parents=True)
        self.src = self.card / REL
        self.src.write_bytes(os.urandom(150_000))
        (self.card / "CLIPS" / "REC_0000.MOV").write_bytes(os.urandom(40_000))   # an earlier, finished recording
        old = 1_600_000_000
        os.utime(self.card / "CLIPS" / "REC_0000.MOV", (old, old))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dests(self, *names):
        return [str(self.tmp / n) for n in names]

    def test_three_bursts_end_in_one_verified_copy_and_one_mhl_entry(self):
        bursts = [os.urandom(100_000), os.urandom(70_000), os.urandom(130_000)]
        rec = Recorder(self.src, bursts)
        with OpenRecorder() as opens, mock.patch.object(core, "CHUNK", SMALL_CHUNK):
            r = offload(str(self.card), self.dests("ssd1", "ssd2"), follow=True, settle_s=5, poll_s=1, clock=rec.clock, sleep=rec.sleep)
        final = self.src.read_bytes()
        self.assertEqual(len(final), 150_000 + 300_000)
        self.assertTrue(r.ok, r.to_dict()["destinations"])
        f = r.follow["files"][REL]
        self.assertEqual(f["passes"], 4, "the first look plus one pass per burst")
        self.assertTrue(f["sealed"])
        self.assertTrue(f["reread_at_seal"], "read in several passes: re-hashed once in full before it is sealed")
        self.assertEqual(r.follow["stopped"], "all_sealed")
        self.assertNotIn("CLIPS/REC_0000.MOV", r.follow["files"], "a finished file is copied the ordinary way")
        # Appended bytes only: every byte read once while growing, plus one full re-hash at the seal.
        self.assertEqual(opens.bytes_read[self.src], 2 * len(final))
        self.assertEqual(opens.bytes_read[(self.card / "CLIPS" / "REC_0000.MOV").resolve()], 40_000)
        rec_ = next(x for x in r.files if x.path == REL)
        self.assertEqual((rec_.status, rec_.size, rec_.sha256), ("copied", len(final), hashlib.sha256(final).hexdigest()))
        for d in r.destinations:
            root = Path(d.root)
            self.assertEqual((d.copied, d.growing, d.failed), (2, 0, 0))
            self.assertEqual((root / REL).read_bytes(), final, "one verified copy")
            m = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual([e["path"] for e in m["files"]].count(REL), 1)
            self.assertNotIn("growing", m, "nothing is left growing once it is sealed")
            self.assertEqual(len(mhl_records(root, REL)), 1, "one ASC MHL entry")
            self.assertTrue(verify(str(root))["ok"])
            self.assertEqual((root / REL).stat().st_mtime_ns, self.src.stat().st_mtime_ns)

    def test_a_copy_taken_mid_growth_is_never_sealed(self):
        bursts = [os.urandom(50_000) for _ in range(50)]   # still recording when the follow stops
        rec = Recorder(self.src, bursts)
        r = offload(str(self.card), self.dests("ssd1", "ssd2"), follow=True, settle_s=5, poll_s=1, follow_for_s=3, clock=rec.clock, sleep=rec.sleep)
        self.assertFalse(r.ok)
        self.assertEqual(r.follow["stopped"], "follow_for")
        rec_ = next(x for x in r.files if x.path == REL)
        self.assertEqual(rec_.status, "growing")
        self.assertEqual(rec_.hashes, {}, "no final hash for a file that is not sealed")
        self.assertIn("not sealed", rec_.detail)
        now = self.src.read_bytes()
        for d in r.destinations:
            root = Path(d.root)
            self.assertEqual((d.copied, d.growing), (1, 1))
            m = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertNotIn(REL, [e["path"] for e in m["files"]], "a partial copy is never in files")
            (g,) = m["growing"]
            self.assertEqual((g["path"], g["growing"]), (REL, True))
            partial = (root / REL).read_bytes()
            self.assertEqual(len(partial), g["size"])
            self.assertEqual(partial, now[:g["size"]], "the partial copy is the source's first bytes, usable for editing")
            self.assertEqual(g["sha256"], hashlib.sha256(partial).hexdigest())
            self.assertEqual(mhl_records(root, REL), [], "never in the ASC MHL")
            v = verify(str(root))
            self.assertFalse(v["ok"])
            self.assertEqual((v["growing"], v["changed"], v["unlisted"]), ([REL], [], []))
            self.assertEqual({x["code"] for x in v["failures"]}, {"INCOMPLETE"})
            inv = inventory(str(root))
            self.assertEqual({c["path"]: c["growing"] for c in inv["clips"]}, {"CLIPS/REC_0000.MOV": False, REL: True})
        self.assertEqual(run_cli("verify", r.destinations[0].root, "--json")[0], 3)
        # The recording ends; the next --follow run continues each partial copy from its verified size and seals it.
        with open(self.src, "ab") as f:
            f.write(os.urandom(10_000))
        rec2 = Recorder(self.src, [])
        with OpenRecorder() as opens:
            r2 = offload(str(self.card), self.dests("ssd1", "ssd2"), follow=True, settle_s=5, poll_s=1, clock=rec2.clock, sleep=rec2.sleep)
        self.assertTrue(r2.ok, r2.to_dict()["destinations"])
        final = self.src.read_bytes()
        for d in r2.destinations:
            root = Path(d.root)
            self.assertEqual((root / REL).read_bytes(), final)
            self.assertEqual(len(mhl_records(root, REL)), 1, "sealed once, in the run that saw it stable")
            self.assertTrue(verify(str(root))["ok"])
            self.assertNotIn("growing", json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8")))
        # One read of the source in that run (it did not change while read), and the finished file found identical.
        self.assertEqual(opens.bytes_read[self.src], len(final))
        self.assertEqual([(d.copied, d.skipped_identical) for d in r2.destinations], [(1, 1)] * 2)

    def test_plain_offload_completes_a_partial_copy_left_by_follow(self):
        rec = Recorder(self.src, [os.urandom(20_000) for _ in range(10)])
        offload(str(self.card), self.dests("ssd1"), follow=True, settle_s=5, poll_s=1, follow_for_s=2, clock=rec.clock, sleep=rec.sleep)
        r = offload(str(self.card), self.dests("ssd1"))   # the recording has ended: no --follow needed
        self.assertTrue(r.ok, r.to_dict())
        root = Path(r.destinations[0].root)
        self.assertEqual((root / REL).read_bytes(), self.src.read_bytes())
        self.assertTrue(verify(str(root))["ok"])
        self.assertEqual(len(mhl_records(root, REL)), 1)

    def test_bytes_rewritten_before_the_end_are_found_at_the_seal(self):
        def rewrite_header():   # e.g. a recorder that updates its header when it closes the file
            with open(self.src, "r+b") as f:
                f.write(b"HEADER-AT-CLOSE")
        rec = Recorder(self.src, [os.urandom(30_000), rewrite_header])
        r = offload(str(self.card), self.dests("ssd1"), follow=True, settle_s=5, poll_s=1, clock=rec.clock, sleep=rec.sleep)
        self.assertTrue(r.ok, r.to_dict())
        self.assertTrue(r.follow["files"][REL]["recopied"])
        root = Path(r.destinations[0].root)
        self.assertEqual((root / REL).read_bytes(), self.src.read_bytes())
        self.assertTrue((root / REL).read_bytes().startswith(b"HEADER-AT-CLOSE"))
        self.assertTrue(verify(str(root))["ok"])
        self.assertIn("copied again in full", next(x for x in r.files if x.path == REL).detail)

    def test_a_different_file_at_the_destination_is_a_conflict_not_appended_to(self):
        other = self.tmp / "ssd1" / "RECORDER" / REL
        other.parent.mkdir(parents=True)
        other.write_bytes(b"someone else's file")
        rec = Recorder(self.src, [os.urandom(10_000)])
        r = offload(str(self.card), self.dests("ssd1", "ssd2"), follow=True, settle_s=5, poll_s=1, clock=rec.clock, sleep=rec.sleep)
        self.assertFalse(r.ok)
        self.assertEqual(other.read_bytes(), b"someone else's file")
        self.assertEqual([(d.conflicts, d.copied) for d in r.destinations], [(1, 1), (0, 2)])
        self.assertEqual((self.tmp / "ssd2" / "RECORDER" / REL).read_bytes(), self.src.read_bytes())

    def test_cli_flags_and_refusals(self):
        code, out = run_cli("offload", str(self.card), "--dest", str(self.tmp / "ssd1"), "--follow", "--settle", "0", "--poll", "0.01", "--json")
        self.assertEqual(code, 0, out)
        d = json.loads(out)
        self.assertEqual(d["follow"]["settle_s"], 0)
        self.assertEqual(d["destinations"][0]["growing"], 0)
        code, out = run_cli("offload", str(self.card), "--dest", str(self.tmp / "ssd2"), "--follow", "--poll", "0", "--json")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "FOLLOW_INVALID"))
        self.assertFalse((self.tmp / "ssd2").exists(), "refused before anything is written")


if __name__ == "__main__":
    unittest.main()
