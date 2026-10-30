"""ingest-skill: offload / verify / inventory on temporary folders. Stdlib only; ffprobe is used when present."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest_skill.cli import main  # noqa: E402
from ingest_skill.contract import contract  # noqa: E402
from ingest_skill import hashing  # noqa: E402
from ingest_skill.core import MANIFEST_NAME, IngestError, doctor, inventory, offload, sha256_file, verify  # noqa: E402

try:
    import xxhash  # optional extra; CI runs the suite without it and again with it
except ImportError:
    xxhash = None

ASCMHL = "urn:ASC:MHL:v2.0"
ASCMHL_DIR = "urn:ASC:MHL:DIRECTORY:v2.0"
# Independent C4 vectors (SMPTE ST 2114), produced by the ASC's reference implementation (ascmhl 1.2, hasher.C4).
C4_EMPTY = "c459dsjfscH38cYeXXYogktxf4Cd9ibshE3BHUo6a58hBXmRQdZrAkZzsWcbWtDg5oQstpDuni4Hirj75GEmTc1sFT"
C4_INGEST_SKILL = "c43z3qXZqwxjzZyrwfMXo2Ev4Dxhps1ZPgDTo71RaUAzE1NFuKcovJA5yM93EupUuhGF2TGqpsTR5reij2whdk6CgV"


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


def flip_byte(path: Path, offset: int = 1000) -> None:
    data = bytearray(path.read_bytes())
    data[offset] ^= 0x01
    path.write_bytes(bytes(data))


def read_mhl(path: Path):
    """(process, {path: (size, {format: (value, action)})}) parsed with the stdlib, namespace-qualified."""
    root = ET.parse(path).getroot()
    assert root.tag == f"{{{ASCMHL}}}hashlist" and root.get("version") == "2.0", root.tag
    q = lambda t: f"{{{ASCMHL}}}{t}"  # noqa: E731
    # Element order of the ASC MHL XML Schema (HashListType / CreatorInfoType / HashType).
    assert [c.tag for c in root] == [q("creatorinfo"), q("processinfo"), q("hashes")], [c.tag for c in root]
    assert [c.tag for c in root.find(q("creatorinfo"))] == [q("creationdate"), q("hostname"), q("tool")]
    records = {}
    for h in root.find(q("hashes")):
        p = h.find(q("path"))
        records[p.text] = (int(p.get("size")), {c.tag.split("}")[1]: (c.text, c.get("action")) for c in h if c is not p})
    return root.find(q("processinfo")).findtext(q("process")), records


def read_chain(folder: Path):
    root = ET.parse(folder / "ascmhl_chain.xml").getroot()
    assert root.tag == f"{{{ASCMHL_DIR}}}ascmhldirectory"
    return [(int(e.get("sequencenr")), e.findtext(f"{{{ASCMHL_DIR}}}path"), e.findtext(f"{{{ASCMHL_DIR}}}c4")) for e in root]


class HashTests(unittest.TestCase):
    def test_c4_matches_the_reference_implementation(self):
        self.assertEqual(hashing.hash_bytes(b"", "c4"), C4_EMPTY)
        self.assertEqual(hashing.hash_bytes(b"ingest-skill", "c4"), C4_INGEST_SKILL)
        self.assertEqual(len(C4_EMPTY), 90)

    @unittest.skipIf(xxhash is None, "xxhash extra not installed")
    def test_xxh64_is_canonical_seed_zero(self):
        self.assertEqual(hashing.hash_bytes(b"", "xxh64"), "ef46db3751d8e999")
        self.assertEqual(hashing.hash_bytes(b"ingest-skill", "xxh64"), "d609813b5bce0ee8")


class AscMhlTests(unittest.TestCase):
    """ASC MHL history per destination and verify, for both offload hashes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.card = make_card(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def algorithms(self):
        return ["sha256", "xxh64"] if xxhash is not None else ["sha256"]

    def expected_mhl_hash(self, data: bytes, algo: str) -> str:
        if algo == "sha256":  # c4 = base58(SHA-512), computed here without the package's own encoder's call path
            n, out = int.from_bytes(hashlib.sha512(data).digest(), "big"), ""
            while n:
                n, r = divmod(n, 58)
                out = hashing.C4_CHARSET[r] + out
            return "c4" + out.rjust(88, "1")
        return xxhash.xxh64(data).hexdigest()

    def test_mhl_written_per_destination_with_correct_hashes(self):
        for algo in self.algorithms():
            with self.subTest(hash=algo):
                fmt = {"sha256": "c4", "xxh64": "xxh64"}[algo]
                r = offload(str(self.card), [str(self.tmp / algo / "ssd1"), str(self.tmp / algo / "ssd2")], hash_algorithm=algo)
                self.assertTrue(r.ok, r.to_dict()["destinations"])
                for d in r.destinations:
                    root = Path(d.root)
                    folder = root / "ascmhl"
                    mhls = sorted(folder.glob("*.mhl"))
                    self.assertEqual([Path(d.mhl)], mhls)
                    self.assertRegex(mhls[0].name, r"^0001_CARD_A_\d{4}-\d\d-\d\d_\d{6}Z\.mhl$")
                    process, records = read_mhl(mhls[0])
                    self.assertEqual(process, "transfer")
                    self.assertEqual(sorted(records), ["DCIM/notes.txt", "PRIVATE/M4ROOT/CLIP/C0001.MP4", "PRIVATE/M4ROOT/CLIP/C0002.MP4"])
                    for rel, (size, hashes) in records.items():
                        data = (self.card / rel).read_bytes()
                        self.assertEqual(size, len(data))
                        self.assertEqual(hashes, {fmt: (self.expected_mhl_hash(data, algo), "original")})
                    self.assertEqual(read_chain(folder), [(1, mhls[0].name, hashing.hash_bytes(mhls[0].read_bytes(), "c4"))])
                    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
                    self.assertEqual(manifest["hash"], algo)
                    c1 = next(e for e in manifest["files"] if e["path"].endswith("C0001.MP4"))
                    want = hashlib.sha256 if algo == "sha256" else xxhash.xxh64
                    self.assertEqual(c1[algo], want((self.card / c1["path"]).read_bytes()).hexdigest())
                    v = verify(str(root))
                    self.assertTrue(v["ok"], v)
                    self.assertEqual((v["hash"], v["checked"], v["unlisted"], v["mhl"]["generations"]), (algo, 3, [], 1))
                    clip = next(c for c in inventory(str(root))["clips"] if c["path"].endswith("C0001.MP4"))
                    self.assertEqual(clip["hash"], {"algorithm": algo, "value": c1[algo]})

    def test_verify_fails_with_a_structured_error_on_a_flipped_byte(self):
        for algo in self.algorithms():
            with self.subTest(hash=algo):
                r = offload(str(self.card), [str(self.tmp / algo)], hash_algorithm=algo)
                root = Path(r.destinations[0].root)
                rel = "PRIVATE/M4ROOT/CLIP/C0001.MP4"
                flip_byte(root / rel)
                v = verify(str(root))
                self.assertFalse(v["ok"])
                self.assertEqual((v["changed"], v["missing"]), ([rel], []))
                got = {(f["code"], f["hash"], f["against"].split("/")[0]) for f in v["failures"] if f["path"] == rel}
                self.assertEqual(got, {("VERIFY_MISMATCH", algo, MANIFEST_NAME), ("VERIFY_MISMATCH", {"sha256": "c4", "xxh64": "xxh64"}[algo], "ascmhl")})
                for f in v["failures"]:
                    self.assertNotEqual(f["expected"], f["actual"])
                code, out = run_cli("verify", str(root), "--json")
                self.assertEqual((code, json.loads(out)["failures"][0]["code"]), (3, "VERIFY_MISMATCH"))

    def test_verify_from_the_ascmhl_history_alone(self):
        for algo in self.algorithms():
            with self.subTest(hash=algo):
                r = offload(str(self.card), [str(self.tmp / algo)], hash_algorithm=algo)
                root = Path(r.destinations[0].root)
                (root / MANIFEST_NAME).unlink()
                v = verify(str(root))
                self.assertTrue(v["ok"], v)
                self.assertEqual((v["hash"], v["checked"]), (None, 3))
                (root / "DCIM/notes.txt").unlink()
                flip_byte(root / "PRIVATE/M4ROOT/CLIP/C0002.MP4", 7)
                v = verify(str(root))
                self.assertFalse(v["ok"])
                self.assertEqual((v["missing"], v["changed"]), (["DCIM/notes.txt"], ["PRIVATE/M4ROOT/CLIP/C0002.MP4"]))
                self.assertEqual({f["code"] for f in v["failures"]}, {"FILE_MISSING", "VERIFY_MISMATCH"})

    def test_resume_appends_a_verified_generation(self):
        offload(str(self.card), [str(self.tmp / "ssd1")])
        r = offload(str(self.card), [str(self.tmp / "ssd1")])
        self.assertTrue(r.ok)
        folder = Path(r.destinations[0].root) / "ascmhl"
        chain = read_chain(folder)
        self.assertEqual([seq for seq, _, _ in chain], [1, 2])
        self.assertTrue(chain[1][1].startswith("0002_CARD_A_"))
        _, records = read_mhl(folder / chain[1][1])
        self.assertEqual({a for _, hashes in records.values() for _, a in hashes.values()}, {"verified"})
        self.assertTrue(verify(r.destinations[0].root)["ok"])

    def test_a_tampered_manifest_breaks_the_chain(self):
        r = offload(str(self.card), [str(self.tmp / "ssd1")])
        m = Path(r.destinations[0].mhl)
        m.write_bytes(m.read_bytes().replace(b"<hostname>", b"<hostname>x"))
        v = verify(r.destinations[0].root)
        self.assertFalse(v["ok"])
        self.assertEqual([p["code"] for p in v["mhl"]["problems"]], ["MHL_INVALID"])
        self.assertIn("MHL_INVALID", {f["code"] for f in v["failures"]})

    def test_source_history_is_carried_into_the_copy(self):
        # A shuttle copy made from a backup: the backup's ascmhl/ continues on the shuttle drive.
        first = Path(offload(str(self.card), [str(self.tmp / "ssd1")]).destinations[0].root)
        r = offload(str(first), [str(self.tmp / "shuttle")])
        self.assertTrue(r.ok, r.to_dict()["destinations"])
        self.assertEqual(sorted(s.status for s in r.skipped), ["skipped_ascmhl", "skipped_manifest"])
        folder = Path(r.destinations[0].root) / "ascmhl"
        chain = read_chain(folder)
        self.assertEqual([seq for seq, _, _ in chain], [1, 2])
        self.assertEqual((folder / chain[0][1]).read_bytes(), (first / "ascmhl" / chain[0][1]).read_bytes())
        _, records = read_mhl(folder / chain[1][1])
        self.assertEqual({a for _, hashes in records.values() for _, a in hashes.values()}, {"verified"})
        again = offload(str(first), [str(self.tmp / "shuttle")])
        self.assertTrue(again.ok, again.to_dict()["destinations"])
        self.assertEqual(again.destinations[0].conflicts, 0)
        self.assertTrue(verify(r.destinations[0].root)["ok"])

    def test_a_card_that_differs_from_its_own_history_is_reported(self):
        first = Path(offload(str(self.card), [str(self.tmp / "ssd1")]).destinations[0].root)
        flip_byte(first / "PRIVATE/M4ROOT/CLIP/C0002.MP4", 3)  # the backup rotted after its history was written
        r = offload(str(first), [str(self.tmp / "shuttle")])
        self.assertFalse(r.ok)
        self.assertEqual(r.destinations[0].mhl_failed, 1)
        _, records = read_mhl(Path(r.destinations[0].mhl))
        self.assertEqual(records["PRIVATE/M4ROOT/CLIP/C0002.MP4"][1]["c4"][1], "failed")

    def test_xxh64_without_the_extra_is_refused_before_writing(self):
        saved = hashing.xxhash
        hashing.xxhash = None
        try:
            with self.assertRaises(IngestError) as cm:
                offload(str(self.card), [str(self.tmp / "ssd1")], hash_algorithm="xxh64")
            self.assertEqual(cm.exception.code, "HASH_UNAVAILABLE")
            self.assertIn("ingest-skill[xxhash]", str(cm.exception))
            self.assertFalse((self.tmp / "ssd1").exists())
            code, out = run_cli("offload", str(self.card), "--dest", str(self.tmp / "ssd1"), "--hash", "xxh64", "--json")
            self.assertEqual((code, json.loads(out)["error"]["code"]), (2, "HASH_UNAVAILABLE"))
            d = doctor()
            self.assertTrue(d["ok"], "a missing optional extra is not a failure")
            self.assertEqual((d["checks"]["xxhash"]["status"], d["hash_algorithms"]["xxh64"], d["hash_algorithms"]["sha256"]), ("unavailable", "unavailable", "ok"))
        finally:
            hashing.xxhash = saved

    @unittest.skipIf(xxhash is None, "xxhash extra not installed")
    def test_verify_of_an_xxh64_manifest_without_the_extra_is_refused(self):
        root = Path(offload(str(self.card), [str(self.tmp / "ssd1")], hash_algorithm="xxh64").destinations[0].root)
        saved = hashing.xxhash
        hashing.xxhash = None
        try:
            with self.assertRaises(IngestError) as cm:
                verify(str(root))
            self.assertEqual(cm.exception.code, "HASH_UNAVAILABLE")
        finally:
            hashing.xxhash = saved


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
        self.assertEqual([o["id"] for o in c["capabilities"]["optional"]], ["ffprobe", "xxhash"])
        for code in ("SOURCE_CHANGED", "VERIFY_MISMATCH", "HASH_UNAVAILABLE", "FILE_MISSING", "MHL_INVALID"):
            self.assertIn(code, c["errors"])

    def test_contract_validates_against_the_os_json_schema(self):
        # ECOSYSTEM_ROADMAP.md step 1: `ingest contract --json` against the OS registry/capability_contract.schema.json.
        # jsonschema is not a dependency: without it (or without the schema) this skips, unless
        # INGEST_REQUIRE_CONTRACT_SCHEMA=1 (set in CI, which installs jsonschema and checks out the OS registry).
        required = os.environ.get("INGEST_REQUIRE_CONTRACT_SCHEMA") == "1"
        try:
            import jsonschema
        except ImportError:
            if required:
                raise
            self.skipTest("jsonschema not installed")
        candidates = [os.environ.get("INGEST_OS_CONTRACT_SCHEMA", ""),
                      str(Path(__file__).resolve().parents[2] / "AI-video-production-OS" / "registry" / "capability_contract.schema.json")]
        schema_path = next((Path(c) for c in candidates if c and Path(c).is_file()), None)
        if schema_path is None:
            if required:
                self.fail(f"no OS capability contract schema at {candidates}")
            self.skipTest("the OS registry/capability_contract.schema.json is not checked out")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        code, out = run_cli("contract", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        errors = sorted(validator_cls(schema).iter_errors(doc), key=lambda e: list(e.path))
        self.assertEqual([f"{list(e.path)}: {e.message}" for e in errors], [])
        # The validator is really checking: a contract with no identity, or a bad lifecycle, is rejected.
        anonymous = {k: v for k, v in doc.items() if k not in ("skill_id", "id", "skill")}
        self.assertFalse(validator_cls(schema).is_valid(anonymous))
        bad = json.loads(out)
        bad["provides"][0]["lifecycle"] = "SHIPPED"
        self.assertFalse(validator_cls(schema).is_valid(bad))

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
