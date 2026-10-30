"""ingest-skill: offload / verify / inventory on temporary folders. Stdlib only; ffprobe is used when present."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest_skill.cli import main  # noqa: E402
from ingest_skill.contract import contract  # noqa: E402
from ingest_skill.core import MANIFEST_NAME, IngestError, inventory, offload, sha256_file, verify  # noqa: E402


def make_card(root: Path) -> Path:
    card = root / "CARD_A"
    (card / "PRIVATE" / "M4ROOT" / "CLIP").mkdir(parents=True)
    (card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0001.MP4").write_bytes(os.urandom(300_000))
    (card / "PRIVATE" / "M4ROOT" / "CLIP" / "C0002.MP4").write_bytes(os.urandom(120_000))
    (card / "DCIM").mkdir()
    (card / "DCIM" / "notes.txt").write_text("slate 1", encoding="utf-8")
    (card / ".DS_Store").write_bytes(b"junk")
    (card / "._C0001.MP4").write_bytes(b"appledouble")
    return card


def run_cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(list(args))
    return code, buf.getvalue()


class OffloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.card = make_card(self.tmp)
        self.before = {p.relative_to(self.card).as_posix(): sha256_file(p) for p in self.card.rglob("*") if p.is_file()}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_source_untouched(self):
        after = {p.relative_to(self.card).as_posix(): sha256_file(p) for p in self.card.rglob("*") if p.is_file()}
        self.assertEqual(after, self.before)

    def test_dual_destination_copies_verify_and_manifest(self):
        r = offload(str(self.card), [str(self.tmp / "ssd1"), str(self.tmp / "ssd2")], project="2026-10-30_学会")
        self.assertTrue(r.ok)
        self.assertEqual([f.path for f in r.files], ["DCIM/notes.txt", "PRIVATE/M4ROOT/CLIP/C0001.MP4", "PRIVATE/M4ROOT/CLIP/C0002.MP4"])
        self.assertEqual(sorted(s.status for s in r.skipped), ["skipped_junk", "skipped_junk"])
        for d in r.destinations:
            self.assertEqual((d.copied, d.conflicts, d.failed), (3, 0, 0))
            root = Path(d.root)
            self.assertEqual(root.parts[-2:], ("2026-10-30_学会", "CARD_A"))
            self.assertEqual(sha256_file(root / "PRIVATE/M4ROOT/CLIP/C0001.MP4"), self.before["PRIVATE/M4ROOT/CLIP/C0001.MP4"])
            self.assertFalse(list(root.rglob("*.part")), "no temporary files left behind")
            self.assertTrue(verify(str(root))["ok"])
        self.assert_source_untouched()

    def test_rerun_is_a_resume(self):
        offload(str(self.card), [str(self.tmp / "ssd1")])
        r = offload(str(self.card), [str(self.tmp / "ssd1")])
        self.assertTrue(r.ok)
        self.assertEqual((r.destinations[0].copied, r.destinations[0].skipped_identical), (0, 3))

    def test_existing_different_file_is_a_conflict_never_overwritten(self):
        dst = self.tmp / "ssd1" / "CARD_A" / "PRIVATE" / "M4ROOT" / "CLIP" / "C0002.MP4"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"someone else's clip")
        r = offload(str(self.card), [str(self.tmp / "ssd1")])
        self.assertFalse(r.ok)
        self.assertEqual(r.destinations[0].conflicts, 1)
        self.assertEqual(dst.read_bytes(), b"someone else's clip")

    def test_refusals_write_nothing(self):
        for dests, code in (([str(self.card / "inside")], "DESTINATION_OVERLAPS_SOURCE"), ([], "NO_DESTINATION"),
                            ([str(self.tmp / "x"), str(self.tmp / "x")], "DUPLICATE_DESTINATION")):
            with self.subTest(code=code):
                with self.assertRaises(IngestError) as cm:
                    offload(str(self.card), dests)
                self.assertEqual(cm.exception.code, code)
        self.assertFalse((self.card / "inside").exists())
        with self.assertRaises(IngestError):
            offload(str(self.tmp / "no-such-card"), [str(self.tmp / "ssd1")])

    def test_dry_run_writes_nothing(self):
        r = offload(str(self.card), [str(self.tmp / "ssd1")], dry_run=True)
        self.assertEqual({f.status for f in r.files}, {"planned"})
        self.assertFalse((self.tmp / "ssd1").exists())

    @unittest.skipIf(os.name == "nt", "creating symlinks needs privileges on Windows")
    def test_symlinks_are_not_followed(self):
        outside = self.tmp / "outside.mov"
        outside.write_bytes(b"not on the card")
        (self.card / "link.mov").symlink_to(outside)
        r = offload(str(self.card), [str(self.tmp / "ssd1")])
        self.assertIn("link.mov", [s.path for s in r.skipped if s.status == "skipped_symlink"])
        self.assertFalse((self.tmp / "ssd1" / "CARD_A" / "link.mov").exists())

    def test_verify_detects_missing_changed_unlisted(self):
        r = offload(str(self.card), [str(self.tmp / "ssd1")])
        root = Path(r.destinations[0].root)
        (root / "DCIM" / "notes.txt").write_text("edited", encoding="utf-8")
        (root / "PRIVATE/M4ROOT/CLIP/C0002.MP4").unlink()
        (root / "extra.wav").write_bytes(b"x")
        v = verify(str(root))
        self.assertFalse(v["ok"])
        self.assertEqual((v["changed"], v["missing"], v["unlisted"]), (["DCIM/notes.txt"], ["PRIVATE/M4ROOT/CLIP/C0002.MP4"], ["extra.wav"]))

    def test_cli_exit_codes_and_json(self):
        code, out = run_cli("offload", str(self.card), "--dest", str(self.tmp / "ssd1"), "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        code, out = run_cli("offload", str(self.card), "--dest", str(self.card / "in"), "--json")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "DESTINATION_OVERLAPS_SOURCE"))
        root = self.tmp / "ssd1" / "CARD_A"
        (root / "DCIM" / "notes.txt").write_text("edited", encoding="utf-8")
        code, _ = run_cli("verify", str(root), "--json")
        self.assertEqual(code, 3)


class InventoryAndContractTests(unittest.TestCase):
    def test_contract_has_the_os_registry_keys(self):
        c = contract()
        for k in ("skill_id", "id", "skill", "version", "contract_version", "provides", "dependencies", "not_provided"):
            self.assertIn(k, c)
        self.assertEqual({p["id"] for p in c["provides"]}, {"ingest.offload", "ingest.verify", "ingest.inventory"})
        self.assertFalse(c["execution"]["network"])
        code, out = run_cli("contract")
        self.assertEqual(json.loads(out)["version"], c["version"])

    def test_contract_fits_the_os_registry_schema(self):
        # The parts of the OS registry/capability_contract.schema.json that constrain types, checked with the stdlib.
        c = contract()
        self.assertIsInstance(c["skill"], dict)
        self.assertEqual((c["skill"]["id"], c["skill"]["version"]), (c["skill_id"], c["version"]))
        self.assertIsInstance(c["contract_version"], str)
        for p in c["provides"]:
            self.assertEqual(set(p), {"id", "lifecycle", "tool_id"})
            self.assertIn(p["lifecycle"], ("PROPOSED", "EXPERIMENTAL", "STABLE", "DEPRECATED", "RETIRED"))
        self.assertEqual(len({p["id"] for p in c["provides"]}), len(c["provides"]))
        for d in c["dependencies"]:
            self.assertTrue(d.get("skill_id") and d.get("version_range"), d)
        self.assertEqual([o["id"] for o in c["capabilities"]["optional"]], ["ffprobe"])
        for code in ("SOURCE_CHANGED", "VERIFY_MISMATCH"):
            self.assertIn(code, c["errors"])

    def test_inventory_lists_media_with_manifest_hashes(self):
        tmp = Path(tempfile.mkdtemp())
        card = make_card(tmp)
        if shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2", "-f", "lavfi", "-i", "sine=duration=2",
                            "-shortest", "-c:v", "libx264", "-c:a", "aac", "-timecode", "01:00:00:00", str(card / "DCIM" / "A001.mov")], check=True)
        r = offload(str(card), [str(tmp / "ssd1")])
        inv = inventory(r.destinations[0].root)
        paths = [c["path"] for c in inv["clips"]]
        self.assertIn("PRIVATE/M4ROOT/CLIP/C0001.MP4", paths)
        self.assertNotIn("DCIM/notes.txt", paths)
        self.assertTrue(all(c["sha256"] for c in inv["clips"]), "hashes come from the manifest")
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            mov = next(c for c in inv["clips"] if c["path"] == "DCIM/A001.mov")
            self.assertTrue(mov["probed"])
            self.assertEqual((mov["width"], mov["height"], mov["timecode"]), (320, 240, "01:00:00:00"))
            self.assertAlmostEqual(mov["duration_s"], 2.0, delta=0.2)
            fake = next(c for c in inv["clips"] if c["path"].endswith("C0001.MP4"))
            self.assertFalse(fake["probed"], "random bytes are not a readable clip: unknown, not zero")
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
