"""Roadmap step 10 / ADR-009: `offload --progress` reports bytes, MB/s per destination and the ETA as JSON lines, and
warns, while the copy runs, about a destination much slower than the others. A slow drive is simulated by delaying
every write to it (the open() wrapper of test_fanout).

The simulated drives are folders of one real disk, so the real time of a write is the same for all of them. On a busy
machine that shared time can be larger than the delays (seen on a macOS CI runner: every write took tens of
milliseconds), and it hides the difference between the drives. The tests that compare drive speeds therefore give the
writer threads a clock on which a write takes exactly its drive's delay (DriveClock)."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_skill import core  # noqa: E402
from ingest_skill.cli import main  # noqa: E402
from ingest_skill.core import offload  # noqa: E402
from test_fanout import OpenRecorder  # noqa: E402

SMALL_CHUNK = 64 * 1024


class DriveClock:
    """Replaces the time module inside ingest_skill.core. perf_counter(), which the writer threads use to time a write,
    counts only the delays of the calling thread's own simulated drive; every other name is the real time module's."""

    def __init__(self):
        self._local = threading.local()

    def advance(self, seconds):
        self._local.now = self.perf_counter() + seconds

    def perf_counter(self):
        return getattr(self._local, "now", 0.0)

    def __getattr__(self, name):
        return getattr(time, name)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.card = self.tmp / "CARD_A"
        (self.card / "CLIP").mkdir(parents=True)
        (self.card / "CLIP" / "C0001.MP4").write_bytes(os.urandom(1_200_000))
        (self.card / "CLIP" / "C0002.MP4").write_bytes(os.urandom(300_000))
        (self.card / "CLIP" / "C0003.MP4").write_bytes(b"")
        self.total = 1_500_000

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_offload(self, dests, delays=None, **kw):
        """delays: destination name -> seconds every write of a chunk takes (a drive of that speed). The write really
        waits that long, so the copy runs at the pace of the slowest drive, and the writer's clock (DriveClock) counts
        exactly that long, whatever the real disk under the test does."""
        events = []
        clock = DriveClock()
        with OpenRecorder() as rec, mock.patch.object(core, "CHUNK", SMALL_CHUNK), mock.patch.object(core, "time", clock if delays else time):
            for name, delay in (delays or {}).items():
                d = self.tmp / name
                rec.faults.append((lambda p, mode, d=d: "w" in mode and d in p.parents and p.name.endswith(".part"),
                                   {"write_delay": delay, "on_write": clock.advance}))
            r = offload(str(self.card), [str(self.tmp / d) for d in dests], progress=events.append, progress_interval_s=0, **kw)
        return r, events

    def assert_monotonic(self, events):
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "done")
        for key in ("bytes_done", "files_done", "elapsed_s"):
            values = [e[key] for e in events]
            self.assertEqual(values, sorted(values), f"{key} never goes back")
        self.assertEqual(events[-1]["bytes_done"], self.total)
        self.assertEqual(events[-1]["bytes_total"], self.total)
        self.assertEqual(events[-1]["files_done"], 3)
        self.assertEqual(events[-1]["eta_s"], 0.0)

    def test_monotonic_progress_with_speed_per_destination_and_eta(self):
        r, events = self.run_offload(["ssd1", "ssd2"])
        self.assertTrue(r.ok)
        self.assert_monotonic(events)
        progress = [e for e in events if e["event"] == "progress"]
        self.assertGreaterEqual(len(progress), 20, "one line per chunk with a zero interval")
        for e in progress:
            self.assertEqual(set(e) >= {"file", "file_bytes", "file_size", "mb_s", "eta_s", "destinations"}, True)
            self.assertLessEqual(e["file_bytes"], e["file_size"])
            self.assertEqual([d["root"] for d in e["destinations"]], [d.root for d in r.destinations])
        self.assertTrue(all(e["eta_s"] is not None and e["eta_s"] >= 0 for e in progress if e["mb_s"]))
        files = [e for e in events if e["event"] == "file"]
        self.assertEqual([(e["file"], e["status"]) for e in files], [("CLIP/C0001.MP4", "copied"), ("CLIP/C0002.MP4", "copied"), ("CLIP/C0003.MP4", "copied")])
        for d in r.destinations:
            self.assertEqual(d.bytes_written, self.total)
            self.assertIsNotNone(d.write_mb_s)
        self.assertFalse([e for e in events if e["event"] == "slow_destination"])
        for line in events:   # every event is one JSON line
            self.assertEqual(json.loads(json.dumps(line)), line)

    def test_a_throttled_destination_is_warned_about_while_the_copy_runs(self):
        # 64 kB chunks in 10 ms, 50 ms and 10 ms: 6.6, 1.3 and 6.6 MB/s on the drives' clock.
        r, events = self.run_offload(["ssd1", "ssd2", "ssd3"], delays={"ssd1": 0.01, "ssd2": 0.05, "ssd3": 0.01}, slow_min_bytes=256 * 1024)
        self.assertTrue(r.ok, "slow is not failed: every copy is still verified")
        self.assert_monotonic(events)
        slow = [e for e in events if e["event"] == "slow_destination"]
        self.assertEqual([e["root"] for e in slow], [r.destinations[1].root], "warned once, about ssd2 only")
        first_file = next(i for i, e in enumerate(events) if e["event"] == "file")
        self.assertLess(events.index(slow[0]), first_file, "raised while the first (1.2 MB) file is still being copied")
        self.assertEqual(slow[0]["dest_mb_s"], 1.3)
        # Judged against both fast drives (median 6.6), or against one if the other's thread has not written 256 kB yet.
        self.assertIn(slow[0]["median_mb_s"], (6.6, round((1.3 + 6.6) / 2, 1)))
        self.assertLess(slow[0]["dest_mb_s"], 0.5 * slow[0]["median_mb_s"])
        self.assertIn("sets the pace", slow[0]["detail"])
        self.assertEqual([any("MB/s" in w for w in d.warnings) for d in r.destinations], [False, True, False])
        # The whole card: 1.5 MB in 24 writes (the last chunk of a file is a short one).
        self.assertEqual([d.write_mb_s for d in r.destinations], [6.2, 1.2, 6.2])

    def test_equally_slow_destinations_are_not_warned_about(self):
        r, events = self.run_offload(["ssd1", "ssd2"], delays={"ssd1": 0.01, "ssd2": 0.01}, slow_min_bytes=256 * 1024)
        self.assertTrue(r.ok)
        self.assertFalse([e for e in events if e["event"] == "slow_destination"])
        self.assertEqual([d.write_mb_s for d in r.destinations], [6.2, 6.2])

    def test_resume_reads_and_counts_but_writes_nothing(self):
        self.run_offload(["ssd1"])
        r, events = self.run_offload(["ssd1"])
        self.assert_monotonic(events)
        self.assertEqual([e["status"] for e in events if e["event"] == "file"], ["skipped_identical"] * 3)
        self.assertEqual((r.destinations[0].bytes_written, r.destinations[0].write_mb_s), (0, None))

    def test_follow_has_no_eta(self):
        r, events = self.run_offload(["ssd1"], follow=True, settle_s=0, poll_s=0.01)
        self.assertTrue(r.ok)
        self.assertTrue(all(e["eta_s"] is None for e in events))
        self.assertEqual(events[-1]["files_done"], 3)

    def test_cli_writes_json_lines_to_stderr_and_one_report_to_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["offload", str(self.card), "--dest", str(self.tmp / "ssd1"), "--progress", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["destinations"][0]["bytes_written"], self.total)
        lines = [json.loads(x) for x in err.getvalue().splitlines()]
        self.assertEqual((lines[0]["event"], lines[-1]["event"], lines[-1]["bytes_done"]), ("start", "done", self.total))


if __name__ == "__main__":
    unittest.main()
