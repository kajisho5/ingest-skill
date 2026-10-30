"""Roadmap step 5 / ADR-004: the source is read once per file for every destination, and a destination that fails
does not affect the others. Opens and reads are counted by wrapping open(); faults are injected the same way."""
from __future__ import annotations

import builtins
import errno
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # test_ingest, also when run as a single file
from ingest_skill import core, hashing  # noqa: E402
from ingest_skill.core import MANIFEST_NAME, offload, verify  # noqa: E402
from test_ingest import make_card, run_cli  # noqa: E402

try:
    import xxhash
except ImportError:
    xxhash = None

FILES = ["DCIM/notes.txt", "PRIVATE/M4ROOT/CLIP/C0001.MP4", "PRIVATE/M4ROOT/CLIP/C0002.MP4"]
BIG = "PRIVATE/M4ROOT/CLIP/C0001.MP4"
SMALL_CHUNK = 64 * 1024   # so that the 300 kB clip is several chunks and a fault lands mid-copy


class _Tracked:
    """A file object that counts the bytes read through it and can fail or stall on write / read."""

    def __init__(self, f, rec: "OpenRecorder", key: Path, fail_write_after=None, fail_read_after=None, on_read=None, write_delay=0.0):
        self._f, self._rec, self._key = f, rec, key
        self._writes = self._reads = 0
        self._fail_write_after, self._fail_read_after, self._on_read, self._write_delay = fail_write_after, fail_read_after, on_read, write_delay

    def read(self, *a):
        if self._fail_read_after is not None and self._reads >= self._fail_read_after:
            raise OSError(errno.EIO, "Input/output error (card reader unplugged)")
        data = self._f.read(*a)
        self._reads += 1
        self._rec.bytes_read[self._key] += len(data)
        if self._on_read:
            self._on_read(self._reads)
        return data

    def write(self, data):
        if self._fail_write_after is not None and self._writes >= self._fail_write_after:
            raise OSError(errno.ENOSPC, "No space left on device")
        if self._write_delay:
            time.sleep(self._write_delay)
        self._writes += 1
        return self._f.write(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False

    def __iter__(self):
        return iter(self._f)

    def __getattr__(self, name):
        return getattr(self._f, name)


class OpenRecorder:
    """Patches builtins.open and io.open: counts every open of a path (by mode) and every byte read from it."""

    def __init__(self):
        self.opens = Counter()       # (path, "r" | "w") -> count
        self.bytes_read = Counter()  # path -> bytes
        self.faults = []             # (predicate(path, mode), kwargs for _Tracked)
        self._real = builtins.open

    def open(self, file, mode="r", *args, **kwargs):
        f = self._real(file, mode, *args, **kwargs)
        if isinstance(file, int):
            return f
        key = Path(os.fspath(file)).resolve()
        self.opens[(key, "w" if any(c in mode for c in "wax+") else "r")] += 1
        extra = dict(next((kw for pred, kw in self.faults if pred(key, mode)), {}))
        if extra.pop("fail_open", False):
            f.close()
            raise PermissionError(errno.EACCES, "Permission denied", str(file))
        return _Tracked(f, self, key, **extra) if "b" in mode else f

    def __enter__(self):
        self._patches = [mock.patch("builtins.open", self.open), mock.patch("io.open", self.open)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def c4(data: bytes) -> str:
    n, out = int.from_bytes(hashlib.sha512(data).digest(), "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = hashing.C4_CHARSET[r] + out
    return "c4" + out.rjust(88, "1")


class SingleReadFanOutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()   # macOS /private/var, Windows 8.3 names
        self.card = make_card(self.tmp)
        self.src = {rel: (self.card / rel).resolve() for rel in FILES}
        self.data = {rel: p.read_bytes() for rel, p in self.src.items()}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dests(self, *names):
        return [str(self.tmp / n) for n in names]

    def assert_each_source_read_once(self, rec: OpenRecorder):
        for rel, p in self.src.items():
            self.assertEqual(rec.opens[(p, "r")], 1, f"{rel} opened {rec.opens[(p, 'r')]} times")
            self.assertEqual(rec.bytes_read[p], len(self.data[rel]), f"{rel}: read {rec.bytes_read[p]} bytes for {len(self.data[rel])}")
            self.assertEqual(rec.opens[(p, "w")], 0, "the source is never opened for writing")

    def test_source_read_once_per_file_for_any_number_of_destinations(self):
        for n in (1, 2, 3):
            with self.subTest(destinations=n):
                dests = self.dests(*(f"n{n}_ssd{i}" for i in range(1, n + 1)))
                with OpenRecorder() as rec, mock.patch.object(core, "CHUNK", SMALL_CHUNK):
                    r = offload(str(self.card), dests)
                self.assertTrue(r.ok, r.to_dict()["destinations"])
                self.assert_each_source_read_once(rec)
                for d in r.destinations:
                    self.assertEqual(d.copied, 3)
                    for rel in FILES:   # the safety guarantee stays: every destination is read back once
                        self.assertEqual(rec.opens[((Path(d.root) / rel).resolve(), "r")], 1, f"{d.root}/{rel} read-back")
                # A resume (every destination has every file) and a mixed run (one new destination) read it once too.
                with OpenRecorder() as rec:
                    again = offload(str(self.card), dests + self.dests(f"n{n}_new"))
                self.assertTrue(again.ok)
                self.assert_each_source_read_once(rec)
                self.assertEqual([(d.copied, d.skipped_identical) for d in again.destinations], [(0, 3)] * n + [(3, 0)])

    def test_outputs_byte_identical_and_hashes_unchanged(self):
        algos = ["sha256", "xxh64"] if xxhash is not None else ["sha256"]
        for algo in algos:
            with self.subTest(hash=algo):
                fmt = hashing.MHL_FORMAT_FOR[algo]
                with mock.patch.object(core, "CHUNK", SMALL_CHUNK):
                    r = offload(str(self.card), self.dests(f"{algo}/a", f"{algo}/b", f"{algo}/c"), hash_algorithm=algo)
                self.assertTrue(r.ok)
                for f in r.files:   # the digests taken in the single pass are the digests of the whole file
                    data = self.data[f.path]
                    want = {"sha256": hashlib.sha256(data).hexdigest(), "c4": c4(data)} if algo == "sha256" else {"xxh64": xxhash.xxh64(data).hexdigest()}
                    self.assertEqual(f.hashes, want)
                    self.assertEqual(f.sha256, want.get("sha256"))
                manifests = []
                for d in r.destinations:
                    root = Path(d.root)
                    for rel in FILES:
                        self.assertEqual((root / rel).read_bytes(), self.data[rel])
                    m = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
                    self.assertNotIn("failed", m)
                    manifests.append([{k: v for k, v in e.items()} for e in m["files"]])
                    self.assertEqual({e["path"]: e[algo] for e in m["files"]}, {f.path: f.hashes[algo] for f in r.files})
                    self.assertTrue(verify(str(root))["ok"])
                    self.assertTrue(Path(d.mhl).is_file() and fmt in Path(d.mhl).read_text(encoding="utf-8"))
                self.assertEqual(manifests[0], manifests[1])
                self.assertEqual(manifests[0], manifests[2])

    def test_one_destination_failing_mid_copy_leaves_the_others_complete_and_verified(self):
        dests = self.dests("ssd1", "ssd2", "ssd3")
        bad = self.tmp / "ssd2" / "CARD_A" / BIG
        with OpenRecorder() as rec, mock.patch.object(core, "CHUNK", SMALL_CHUNK):
            # ssd2 fills up after two chunks of the 300 kB clip have been written.
            rec.faults.append((lambda p, mode: "w" in mode and p.parent == bad.parent and p.name.startswith(f".{bad.name}.") and p.name.endswith(".part"),
                               {"fail_write_after": 2}))
            r = offload(str(self.card), dests)
        self.assert_each_source_read_once(rec)
        self.assertFalse(r.ok)
        self.assertEqual([(d.copied, d.failed) for d in r.destinations], [(3, 0), (2, 1), (3, 0)])
        big = next(f for f in r.files if f.path == BIG)
        self.assertEqual(big.status, "failed")
        self.assertIn("No space left on device", big.detail)
        self.assertIn(str(bad), big.detail, "the detail names the destination that failed")
        self.assertEqual(big.sha256, hashlib.sha256(self.data[BIG]).hexdigest(), "the source hash is still known")
        for i in (0, 2):   # the others: complete, byte-identical, listed in their manifest and ASC MHL, verified
            root = Path(r.destinations[i].root)
            self.assertEqual((root / BIG).read_bytes(), self.data[BIG])
            m = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(sorted(e["path"] for e in m["files"]), FILES)
            self.assertNotIn("failed", m)
            self.assertIn(BIG, Path(r.destinations[i].mhl).read_text(encoding="utf-8"))
            v = verify(str(root))
            self.assertTrue(v["ok"], v)
            self.assertEqual((v["checked"], v["copy_failed"]), (3, []))
        root = Path(r.destinations[1].root)   # the failed one: nothing partial, and it says so
        self.assertFalse(bad.exists())
        self.assertEqual([p for p in root.rglob("*") if p.name.endswith(".part")], [])
        m = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(sorted(e["path"] for e in m["files"]), [p for p in FILES if p != BIG])
        self.assertEqual([(e["path"], e["size"]) for e in m["failed"]], [(BIG, len(self.data[BIG]))])
        self.assertIn("No space left on device", m["failed"][0]["detail"])
        self.assertNotIn(BIG, Path(r.destinations[1].mhl).read_text(encoding="utf-8"))
        v = verify(str(root))
        self.assertFalse(v["ok"])
        self.assertEqual((v["copy_failed"], v["missing"], v["changed"]), ([BIG], [], []))
        self.assertEqual([(f["code"], f["path"], f["against"]) for f in v["failures"]], [("INCOMPLETE", BIG, MANIFEST_NAME)])
        code, _ = run_cli("verify", str(root), "--json")
        self.assertEqual(code, 3)
        # Resume once the drive has space: only the missing file is written, the source is again read once.
        with OpenRecorder() as rec:
            again = offload(str(self.card), dests)
        self.assertTrue(again.ok, again.to_dict()["destinations"])
        self.assert_each_source_read_once(rec)
        self.assertEqual([(d.copied, d.skipped_identical) for d in again.destinations], [(0, 3), (1, 2), (0, 3)])
        self.assertEqual(bad.read_bytes(), self.data[BIG])
        self.assertNotIn("failed", json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8")))
        self.assertTrue(verify(str(root))["ok"])

    def test_a_destination_that_cannot_be_opened_does_not_stop_the_others(self):
        target = self.tmp / "ssd1" / "CARD_A" / "DCIM"
        with OpenRecorder() as rec:
            rec.faults.append((lambda p, mode: "w" in mode and p.parent == target, {"fail_open": True}))
            r = offload(str(self.card), self.dests("ssd1", "ssd2"))
        self.assert_each_source_read_once(rec)
        self.assertEqual([(d.copied, d.failed) for d in r.destinations], [(2, 1), (3, 0)])
        self.assertEqual(list(target.iterdir()), [], "no temporary file is left behind")
        self.assertTrue(verify(r.destinations[1].root)["ok"])

    def test_a_slow_destination_holds_back_the_read_but_corrupts_nothing(self):
        slow = self.tmp / "slow"
        with OpenRecorder() as rec, mock.patch.object(core, "CHUNK", 16 * 1024), mock.patch.object(core, "QUEUE_DEPTH", 1):
            rec.faults.append((lambda p, mode: "w" in mode and slow in p.parents and p.name.endswith(".part"), {"write_delay": 0.002}))
            r = offload(str(self.card), self.dests("fast", "slow"))
        self.assertTrue(r.ok)
        self.assert_each_source_read_once(rec)
        for d in r.destinations:
            for rel in FILES:
                self.assertEqual((Path(d.root) / rel).read_bytes(), self.data[rel])

    def test_a_source_read_error_fails_the_file_on_every_destination_and_leaves_nothing(self):
        with OpenRecorder() as rec, mock.patch.object(core, "CHUNK", SMALL_CHUNK):
            rec.faults.append((lambda p, mode: p == self.src[BIG] and "r" in mode, {"fail_read_after": 2}))
            r = offload(str(self.card), self.dests("ssd1", "ssd2"))
        self.assertFalse(r.ok)
        self.assertEqual([(d.copied, d.failed) for d in r.destinations], [(2, 1), (2, 1)])
        big = next(f for f in r.files if f.path == BIG)
        self.assertEqual((big.status, big.hashes), ("failed", {}))
        self.assertIn("Input/output error", big.detail)
        for d in r.destinations:
            root = Path(d.root)
            self.assertFalse((root / BIG).exists())
            self.assertEqual([p for p in root.rglob("*") if p.name.endswith(".part")], [])
            self.assertEqual(json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))["failed"][0]["path"], BIG)

    def test_a_source_that_changes_while_it_is_read_is_kept_nowhere(self):
        src = self.src[BIG]

        def grow(reads):
            if reads == 1:
                with io.FileIO(src, "ab") as f:   # not through the patched open()
                    f.write(b"appended by the camera")

        with OpenRecorder() as rec, mock.patch.object(core, "CHUNK", SMALL_CHUNK):
            rec.faults.append((lambda p, mode: p == src and "r" in mode, {"on_read": grow}))
            r = offload(str(self.card), self.dests("ssd1", "ssd2"))
        big = next(f for f in r.files if f.path == BIG)
        self.assertEqual(big.status, "failed")
        self.assertIn("changed while it was being copied", big.detail)
        self.assertEqual([(d.copied, d.failed) for d in r.destinations], [(2, 1), (2, 1)])
        for d in r.destinations:
            self.assertFalse((Path(d.root) / BIG).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
