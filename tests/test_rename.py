"""ingest rename (ADR-015): template names, preview, apply, undo, on real files in temporary folders. Stdlib only."""
from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest_skill import mhl, rename  # noqa: E402
from ingest_skill.cli import main  # noqa: E402
from ingest_skill.contract import contract  # noqa: E402
from ingest_skill.core import MANIFEST_NAME, IngestError, inventory, offload, verify  # noqa: E402

TEMPLATE = rename.DEFAULT_TEMPLATE


def write(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def run_cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(list(args))
    return code, buf.getvalue()


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree(root: Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def bwf(path: Path, date: str, time: str) -> Path:
    """A one-second silent 16-bit WAV with a Broadcast Wave bext chunk (origination date and time)."""
    def chunk(cid: bytes, data: bytes) -> bytes:
        return cid + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    bext = (b"talk".ljust(256, b"\x00") + b"Recorder".ljust(32, b"\x00") + b"REF".ljust(32, b"\x00") + date.encode() + time.encode()
            + struct.pack("<Q", 0) + struct.pack("<H", 1) + b"\x00" * 254)
    body = chunk(b"fmt ", fmt) + chunk(b"bext", bext) + chunk(b"data", b"\x00\x00" * 8000)
    return write(path, b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def offloaded(self, files, name="CAM_B"):
        """An offloaded copy of a card holding `files` ({relative path: bytes}); returns the copy's folder."""
        card = self.base / "cards" / name
        for rel, data in files.items():
            write(card / rel, data)
        (self.base / "ssd").mkdir(exist_ok=True)
        r = offload(str(card), [str(self.base / "ssd")])
        self.assertTrue(r.ok)
        return self.base / "ssd" / name

    def fields(self, clips) -> str:
        p = self.base / "fields.json"
        p.write_text(json.dumps({"schema": rename.FIELDS_SCHEMA, "clips": clips}, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def apply(self, folder, **opts):
        p = rename.plan(str(folder), **opts)
        return p, rename.apply(str(folder), p["plan_id"], **opts)


class TemplateTests(unittest.TestCase):
    def test_the_example_name_and_missing_fields_dropped_with_their_separator(self):
        parts = rename.parse_template(TEMPLATE)
        full = {"date": "2026-11-22", "event": "ConfName", "room": "Room-A", "camera": "CamB", "scene": "S01", "speaker": "Speaker-Tanaka", "seq": "0001"}
        self.assertEqual(rename.compose(parts, full), "2026-11-22_ConfName_Room-A_CamB_S01_Speaker-Tanaka_0001")
        for gone, expect in (("room", "2026-11-22_ConfName_CamB_S01_Speaker-Tanaka_0001"), ("date", "ConfName_Room-A_CamB_S01_Speaker-Tanaka_0001"),
                             ("seq", "2026-11-22_ConfName_Room-A_CamB_S01_Speaker-Tanaka")):
            self.assertEqual(rename.compose(parts, {k: v for k, v in full.items() if k != gone}), expect)
        self.assertEqual(rename.compose(parts, {"event": "ConfName", "seq": "0007"}), "ConfName_0007")

    def test_specs(self):
        from datetime import datetime
        dt = datetime(2026, 11, 22, 9, 5, 7)
        self.assertEqual(rename.render_field("date", "", dt), "2026-11-22")
        self.assertEqual(rename.render_field("date", "%Y%m%d", dt), "20261122")
        self.assertEqual(rename.render_field("time", "", dt), "090507")
        self.assertEqual(rename.render_field("scene", "S02", 1), "S01")
        self.assertEqual(rename.render_field("seq", "04", 12), "0012")
        self.assertEqual(rename.render_field("speaker", "Speaker-", "Ken Tanaka"), "Speaker-Ken-Tanaka")
        self.assertEqual(rename.render_field("speaker", "Speaker-", ""), "")

    def test_bad_templates_are_refused(self):
        for t in ("{date}_{event}", "{date}/{event}{ext}", "{ext}", "{event}{ext}{ext}", "a{b{ext}", "{event}<x>{ext}"):
            with self.assertRaises(IngestError) as cm:
                rename.parse_template(t)
            self.assertEqual(cm.exception.code, "TEMPLATE_INVALID", t)

    def test_safe_values_and_stems(self):
        self.assertEqual(rename.safe_value('a<b>:c"d/e\\f|g?h*i'), "a-b-c-d-e-f-g-h-i")
        self.assertEqual(rename.safe_value("  Room\tA \n"), "Room-A")
        self.assertEqual(rename.safe_value("tab\x00bell\x07"), "tab-bell")
        for reserved in ("CON", "nul", "Com1", "LPT9", "aux"):
            self.assertEqual(rename.safe_stem(reserved, 4), reserved + "_")
        self.assertEqual(rename.safe_stem("CON.backup", 4), "CON_.backup")
        self.assertEqual(rename.safe_stem("CONSOLE", 4), "CONSOLE")
        self.assertEqual(rename.safe_stem("name. ", 4), "name")
        long = "東京国際会議場" * 40          # 3 bytes a character
        stem = rename.safe_stem(long, len(".mp4"))
        self.assertLessEqual(len((stem + "_999.mp4").encode("utf-8")), 255)
        self.assertTrue(long.startswith(stem))

    def test_japanese_values_are_nfc(self):
        nfd = unicodedata.normalize("NFD", "デジタル・プロダクション")
        self.assertNotEqual(nfd, unicodedata.normalize("NFC", nfd))
        out = rename.safe_value(nfd)
        self.assertEqual(out, unicodedata.normalize("NFC", out))
        self.assertEqual(out, "デジタル・プロダクション")


class RenameTests(Base):
    def test_the_full_example_name_from_fields_and_operator_values(self):
        copy = self.offloaded({"DCIM/C0001.MP4": os.urandom(4000), "DCIM/C0002.MP4": os.urandom(3000)})
        f = self.fields({"DCIM/C0001.MP4": {"date": "2026-11-22T10:00:00", "scene": 1, "speaker": {"value": "Tanaka", "source": "entity memory"}},
                         "DCIM/C0002.MP4": {"date": "2026-11-22T10:30:00", "scene": 2}})
        opts = dict(values={"event": "ConfName", "room": "Room A", "camera": "CamB"}, fields_file=f)
        p, r = self.apply(copy, **opts)
        rows = {row["from"]: row for row in p["rows"]}
        self.assertEqual(rows["DCIM/C0001.MP4"]["to"], "DCIM/2026-11-22_ConfName_Room-A_CamB_S01_Speaker-Tanaka_0001.mp4")
        self.assertEqual(rows["DCIM/C0002.MP4"]["to"], "DCIM/2026-11-22_ConfName_Room-A_CamB_S02_0002.mp4")
        self.assertEqual(rows["DCIM/C0001.MP4"]["fields"]["speaker"]["source"], "entity memory")
        self.assertEqual(rows["DCIM/C0001.MP4"]["fields"]["room"]["source"], "operator (--set room)")
        self.assertIn("speaker", rows["DCIM/C0002.MP4"]["missing"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["moved"], 2)
        self.assertTrue((copy / "DCIM/2026-11-22_ConfName_Room-A_CamB_S01_Speaker-Tanaka_0001.mp4").is_file())
        v = verify(str(copy))
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["unlisted"], [])
        inv = inventory(str(copy))
        self.assertTrue(all(c["hash"] for c in inv["clips"]), "the manifest hashes follow the new paths")

    def test_preview_writes_nothing_and_apply_needs_its_plan_id(self):
        copy = self.offloaded({"a/DSC_1234.MOV": os.urandom(1000)})
        before = {p: sha(copy / p) for p in tree(copy)}
        code, out = run_cli("rename", "plan", str(copy), "--set", "event=Conf", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], rename.PLAN_SCHEMA)
        self.assertEqual(doc["counts"]["rename"], 1)
        self.assertEqual({p: sha(copy / p) for p in tree(copy)}, before, "plan writes nothing")
        code, out = run_cli("rename", "apply", str(copy), "--set", "event=Conf", "--plan-id", "0" * 16, "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"]["code"], "PLAN_CHANGED")
        code, out = run_cli("rename", "apply", str(copy), "--set", "event=Other", "--plan-id", doc["plan_id"], "--json")
        self.assertEqual(json.loads(out)["error"]["code"], "PLAN_CHANGED", "other options give another plan")
        self.assertEqual({p: sha(copy / p) for p in tree(copy)}, before)
        code, out = run_cli("rename", "apply", str(copy), "--set", "event=Conf", "--plan-id", doc["plan_id"], "--json")
        self.assertEqual(code, 0, out)
        self.assertTrue((copy / "a/Conf_0001.mov").is_file())

    def test_no_metadata_keeps_every_name_and_fallback_fields_only(self):
        copy = self.offloaded({"DCIM/DSC_0001.MOV": os.urandom(800), "DCIM/DSC_0002.MOV": os.urandom(700)})
        before = tree(copy)
        p, r = self.apply(copy)
        self.assertEqual(p["counts"]["rename"], 0)
        self.assertTrue(all(row["status"] == "unchanged" and "no metadata" in row["reason"] for row in p["rows"]))
        self.assertTrue(r["ok"])
        self.assertEqual(r["moved"], 0)
        self.assertEqual(tree(copy), before)
        self.assertFalse((copy / "ingest-rename").exists())
        p, r = self.apply(copy, values={"event": "Summit"})
        self.assertEqual(sorted(row["to"] for row in p["rows"]), ["DCIM/Summit_0001.mov", "DCIM/Summit_0002.mov"])
        self.assertTrue(r["ok"])
        self.assertTrue(verify(str(copy))["ok"])

    def test_collisions_get_a_counter_and_nothing_is_overwritten(self):
        copy = self.offloaded({"DCIM/A.MOV": os.urandom(900), "DCIM/B.MOV": os.urandom(800), "DCIM/Talk.mov": os.urandom(700)})
        # Talk.mov is a clip of its own; with a template of only {event}, every clip asks for the same name
        p, r = self.apply(copy, template="{event}{ext}", values={"event": "Talk"})
        names = sorted(row["to"] for row in p["rows"])
        self.assertEqual(names, ["DCIM/Talk.mov", "DCIM/Talk_2.mov", "DCIM/Talk_3.mov"], p["rows"])
        self.assertTrue(r["ok"])
        self.assertEqual(len(tree(copy / "DCIM")), 3)
        # an existing name in the folder (here, case-insensitively) is never taken
        copy2 = self.offloaded({"x/C1.MOV": os.urandom(500), "x/talk.MOV": os.urandom(400)}, name="CAM_C")
        p = rename.plan(str(copy2), template="{event}{ext}", values={"event": "TALK"})
        rows = {row["from"]: row for row in p["rows"]}
        self.assertEqual(rows["x/C1.MOV"]["to"], "x/TALK_2.mov")
        self.assertEqual(rows["x/talk.MOV"]["to"], "x/TALK.mov", "a clip may take its own name in another case")
        r = rename.apply(str(copy2), p["plan_id"], template="{event}{ext}", values={"event": "TALK"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(os.listdir(copy2 / "x")), ["TALK.mov", "TALK_2.mov"])
        self.assertTrue(verify(str(copy2))["ok"])

    def test_sidecars_keep_their_media_basename(self):
        copy = self.offloaded({"DCIM/100GOPRO/GX010001.MP4": os.urandom(2000), "DCIM/100GOPRO/GX010001.LRF": os.urandom(300),
                               "DCIM/100GOPRO/GX010001.THM": os.urandom(100), "DJI/DJI_0001.MP4": os.urandom(1500), "DJI/DJI_0001.SRT": b"1\n",
                               "flat/C0001.MP4": os.urandom(1200), "flat/C0001M01.XML": b"<x/>", "flat/C0001.XMP": b"<x/>", "flat/notes.txt": b"slate"})
        p, r = self.apply(copy, template="{event}_{seq:03}{ext}", values={"event": "Expo"})
        self.assertTrue(r["ok"], r)
        moved = {f["from"]: f["to"] for row in p["rows"] for f in row["files"]}
        gp = moved["DCIM/100GOPRO/GX010001.MP4"].rsplit(".", 1)[0]
        self.assertEqual(moved["DCIM/100GOPRO/GX010001.LRF"], gp + ".lrf")
        self.assertEqual(moved["DCIM/100GOPRO/GX010001.THM"], gp + ".thm")
        dji = moved["DJI/DJI_0001.MP4"].rsplit(".", 1)[0]
        self.assertEqual(moved["DJI/DJI_0001.SRT"], dji + ".srt")
        c = moved["flat/C0001.MP4"].rsplit(".", 1)[0]
        self.assertEqual(moved["flat/C0001M01.XML"], c + "M01.xml")
        self.assertEqual(moved["flat/C0001.XMP"], c + ".xmp")
        self.assertNotIn("flat/notes.txt", moved, "a file that does not carry the clip's basename stays")
        self.assertTrue((copy / "flat/notes.txt").is_file())
        self.assertTrue(verify(str(copy))["ok"])

    def test_undo_restores_the_original_names_exactly(self):
        files = {"DCIM/DSC_1234.MOV": os.urandom(3000), "DCIM/DSC_1234.XMP": b"<xmp/>", "DCIM/DSC_1235.MOV": os.urandom(2000), "README.txt": b"card"}
        copy = self.offloaded(files)
        before = {p: sha(copy / p) for p in tree(copy) if not p.startswith("ascmhl/") and p != MANIFEST_NAME}
        manifest_before = json.loads((copy / MANIFEST_NAME).read_text(encoding="utf-8"))
        p, r = self.apply(copy, values={"event": "会議", "room": "Hall B"})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("DCIM/DSC_1234.MOV", tree(copy))
        undo_doc = json.loads(Path(r["undo"]).read_text(encoding="utf-8"))
        self.assertEqual(undo_doc["status"], "applied")
        code, out = run_cli("rename", "undo", str(copy), "--json")
        self.assertEqual(code, 0, out)
        after = {p: sha(copy / p) for p in tree(copy) if not p.startswith(("ascmhl/", "ingest-rename/")) and p != MANIFEST_NAME}
        self.assertEqual(after, before, "every file back under its original name, content unchanged")
        manifest = json.loads((copy / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(sorted((e["path"], e["sha256"]) for e in manifest["files"]), sorted((e["path"], e["sha256"]) for e in manifest_before["files"]))
        self.assertEqual([e["action"] for e in manifest["renames"]], ["rename", "undo"])
        v = verify(str(copy))
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["mhl"]["generations"], 3)
        self.assertEqual(json.loads(Path(r["undo"]).read_text(encoding="utf-8"))["status"], "undone")
        code, out = run_cli("rename", "undo", str(copy), "--json")
        self.assertEqual(json.loads(out)["error"]["code"], "UNDO_INVALID", "nothing left to undo")

    def test_an_interrupted_rename_can_be_put_back_with_its_undo_manifest(self):
        copy = self.offloaded({"d/A.MOV": os.urandom(700), "d/B.MOV": os.urandom(600)})
        p = rename.plan(str(copy), values={"event": "E"})
        real = rename._rename
        calls = []

        def stop_after_one(src, dst):   # the process stops after the first rename (a crash, a pulled cable)
            calls.append((src, dst))
            if len(calls) > 1:
                raise KeyboardInterrupt
            return real(src, dst)
        from unittest import mock
        with mock.patch("ingest_skill.rename._rename", side_effect=stop_after_one):
            with self.assertRaises(KeyboardInterrupt):
                rename.apply(str(copy), p["plan_id"], values={"event": "E"})
        pending = next((copy / "ingest-rename").glob("*.json"))
        self.assertEqual(json.loads(pending.read_text(encoding="utf-8"))["status"], "pending")
        self.assertFalse(verify(str(copy))["ok"], "half renamed: the manifest still names the old paths")
        with self.assertRaises(IngestError):
            rename.undo(str(copy))   # the last applied one: there is none
        r = rename.undo(str(copy), str(pending))
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(os.listdir(copy / "d")), ["A.MOV", "B.MOV"])
        self.assertTrue(verify(str(copy))["ok"])

    def test_a_sidecar_goes_with_the_clip_of_the_longest_basename(self):
        copy = self.offloaded({"v/Interview.mp4": os.urandom(500), "v/Interview2.mp4": os.urandom(400), "v/Interview2.xml": b"<x/>",
                               "v/Interview.xml": b"<y/>"})
        p, r = self.apply(copy, template="{event}_{seq:02}{ext}", values={"event": "Talk"})
        self.assertTrue(r["ok"], r)
        moved = {f["from"]: f["to"] for row in p["rows"] for f in row["files"]}
        self.assertEqual(sum(1 for row in p["rows"] for f in row["files"] if f["from"] == "v/Interview2.xml"), 1)
        self.assertEqual(moved["v/Interview2.xml"], moved["v/Interview2.mp4"][:-4] + ".xml")
        self.assertEqual(moved["v/Interview.xml"], moved["v/Interview.mp4"][:-4] + ".xml")
        self.assertTrue(verify(str(copy))["ok"])

    def test_two_clips_differing_only_by_case_never_share_a_name(self):
        probe = self.base / "probe"
        probe.mkdir()
        (probe / "a").write_bytes(b"1")
        if (probe / "A").exists():
            self.skipTest("case-insensitive file system: two names differing only by case cannot exist here")
        copy = self.offloaded({"x/talk.MOV": os.urandom(300), "x/TALK.mov": os.urandom(200)})
        p, r = self.apply(copy, template="{event}{ext}", values={"event": "TALK"})
        self.assertEqual(len({row["to"].casefold() for row in p["rows"]}), 2, p["rows"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(os.listdir(copy / "x")), 2, "nothing overwritten")
        self.assertTrue(verify(str(copy))["ok"])

    def test_rename_never_replaces_another_file(self):
        a = write(self.base / "r" / "a.mov", b"A")
        b = write(self.base / "r" / "b.mov", b"B")
        with self.assertRaises(IngestError):
            rename._rename(a, b)
        self.assertEqual((b.read_bytes(), a.read_bytes()), (b"B", b"A"))
        rename._rename(a, self.base / "r" / "c.mov")
        self.assertEqual((self.base / "r" / "c.mov").read_bytes(), b"A")
        self.assertFalse(a.exists())

    def test_a_damaged_history_stops_the_rename(self):
        copy = self.offloaded({"d/A.MOV": os.urandom(300)})
        mhl_file = next((copy / "ascmhl").glob("0001_*.mhl"))
        mhl_file.write_bytes(mhl_file.read_bytes().replace(b"ingest-skill", b"tampered!!!!"))
        p, r = self.apply(copy, values={"event": "E"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["failures"][0]["code"], "MHL_INVALID")
        self.assertEqual(os.listdir(copy / "d"), ["A.MOV"])

    def test_windows_reserved_and_forbidden_characters(self):
        copy = self.offloaded({"v/a.mov": os.urandom(600), "v/b.mov": os.urandom(500)})
        f = self.fields({"v/a.mov": {"speaker": "CON"}, "v/b.mov": {"speaker": 'Dr. O\'Brien: "Q&A"?'}})
        p, r = self.apply(copy, template="{speaker}{ext}", fields_file=f)
        rows = {row["from"]: row["to"] for row in p["rows"]}
        self.assertEqual(rows["v/a.mov"], "v/CON_.mov")
        self.assertEqual(rows["v/b.mov"], "v/Dr.-O'Brien-Q&A.mov")
        self.assertTrue(r["ok"], r)
        for name in os.listdir(copy / "v"):
            self.assertFalse(set(name) & set('<>:"/\\|?*'), name)
            self.assertNotIn(name.split(".")[0].upper(), rename.WINDOWS_RESERVED)
        self.assertTrue(verify(str(copy))["ok"])

    def test_japanese_names_are_written_in_nfc(self):
        copy = self.offloaded({"m/C0001.MP4": os.urandom(900)})
        nfd = unicodedata.normalize("NFD", "デザイン会議_ガイダンス")
        f = self.fields({"m/C0001.MP4": {"speaker": unicodedata.normalize("NFD", "田中・ジョージ")}})
        p, r = self.apply(copy, values={"event": nfd}, fields_file=f)
        self.assertTrue(r["ok"], r)
        name = p["rows"][0]["to"].split("/")[-1]
        self.assertEqual(name, unicodedata.normalize("NFC", name))
        self.assertEqual(name, "デザイン会議_ガイダンス_Speaker-田中・ジョージ_0001.mp4")
        on_disk = [n for n in os.listdir(copy / "m")]
        self.assertEqual([unicodedata.normalize("NFC", n) for n in on_disk], [name])
        self.assertTrue(verify(str(copy))["ok"])

    def test_capture_date_from_a_broadcast_wave_recorder(self):
        card = self.base / "cards" / "REC"
        bwf(card / "ZOOM0001.WAV", "2026-11-22", "14-03-09")
        (self.base / "ssd").mkdir()
        self.assertTrue(offload(str(card), [str(self.base / "ssd")]).ok)
        copy = self.base / "ssd" / "REC"
        p, r = self.apply(copy, template="{date}_{time}_{event}{ext}", values={"event": "Keynote"})
        row = p["rows"][0]
        self.assertEqual(row["to"], "2026-11-22_140309_Keynote.wav")
        self.assertIn("bwf_bext", row["fields"]["date"]["source"])
        self.assertTrue(r["ok"])

    def test_a_changed_file_stops_the_rename_before_anything_moves(self):
        copy = self.offloaded({"d/A.MOV": os.urandom(1000), "d/B.MOV": os.urandom(1000)})
        p = rename.plan(str(copy), values={"event": "E"})
        target = copy / "d/B.MOV"
        data = bytearray(target.read_bytes())
        data[10] ^= 0xFF
        st = target.stat()
        target.write_bytes(bytes(data))
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))   # same size and mtime: only the content differs
        r = rename.apply(str(copy), p["plan_id"], values={"event": "E"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["failures"][0]["code"], "VERIFY_MISMATCH")
        self.assertEqual(sorted(os.listdir(copy / "d")), ["A.MOV", "B.MOV"])
        self.assertFalse((copy / "ingest-rename").exists())

    def test_the_card_itself_and_incomplete_copies_are_not_renamed(self):
        card = write(self.base / "CARD" / "DSC_1.MOV", os.urandom(100)).parent
        with self.assertRaises(IngestError) as cm:
            rename.plan(str(card), values={"event": "E"})
        self.assertEqual(cm.exception.code, "RENAME_REFUSED")
        copy = self.offloaded({"d/A.MOV": os.urandom(100)})
        doc = json.loads((copy / MANIFEST_NAME).read_text(encoding="utf-8"))
        doc["failed"] = [{"path": "d/B.MOV", "size": 1, "detail": "drive full"}]
        (copy / MANIFEST_NAME).write_text(json.dumps(doc), encoding="utf-8")
        p = rename.plan(str(copy), values={"event": "E"})
        self.assertEqual(p["counts"]["skipped"], 1)
        self.assertIn("incomplete", p["rows"][0]["reason"])

    def test_a_structured_card_is_kept_in_place_and_copied_renamed(self):
        files = {"PRIVATE/M4ROOT/MEDIAPRO.XML": b'<?xml version="1.0"?><MediaProfile xmlns="http://xmlns.sony.net/pro/metadata/mediaprofile"><Contents/></MediaProfile>',
                 "PRIVATE/M4ROOT/CLIP/C0001.MP4": os.urandom(2500), "PRIVATE/M4ROOT/CLIP/C0001M01.XML": b"<NonRealTimeMeta/>",
                 "PRIVATE/M4ROOT/CLIP/C0002.MP4": os.urandom(1500)}
        copy = self.offloaded(files)
        p = rename.plan(str(copy), values={"event": "Conf"})
        self.assertEqual(p["counts"]["skipped"], 2, p["rows"])
        self.assertIn("card structure", p["rows"][0]["reason"])
        out = self.base / "renamed"
        before = {q: sha(copy / q) for q in tree(copy)}
        p, r = self.apply(copy, values={"event": "Conf"}, copy_to=str(out))
        # with no metadata at all, --copy-to still copies every clip, under its own name
        out2 = self.base / "plain"
        p2, r2 = self.apply(copy, copy_to=str(out2))
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(sorted(q for q in tree(out2) if q.endswith((".mp4", ".xml")) and "/" not in q), ["C0001.mp4", "C0001M01.xml", "C0002.mp4"])
        self.assertEqual(p["mode"], "copy")
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(q for q in tree(out) if not q.startswith(("ascmhl/", "ingest-rename/"))),
                         sorted([MANIFEST_NAME, "Conf_0001.mp4", "Conf_0001M01.xml", "Conf_0002.mp4"]))
        self.assertEqual(sha(out / "Conf_0001.mp4"), sha(copy / "PRIVATE/M4ROOT/CLIP/C0001.MP4"))
        self.assertEqual({q: sha(copy / q) for q in tree(copy)}, before, "the folder is only read")
        v = verify(str(out))
        self.assertTrue(v["ok"], v)
        self.assertTrue(verify(str(copy))["ok"])
        with self.assertRaises(IngestError) as cm:
            rename.undo(str(out), r["undo"])
        self.assertEqual(cm.exception.code, "UNDO_INVALID")
        with self.assertRaises(IngestError) as cm:
            rename.plan(str(copy), values={"event": "Conf"}, copy_to=str(copy / "x"))
        self.assertEqual(cm.exception.code, "DESTINATION_OVERLAPS_SOURCE")

    def test_fields_and_sets_are_checked(self):
        copy = self.offloaded({"d/A.MOV": os.urandom(100)})
        for bad in ({"schema": "x", "clips": {}}, {"schema": rename.FIELDS_SCHEMA, "clips": {"d/A.MOV": {"seq": 3}}},
                    {"schema": rename.FIELDS_SCHEMA, "clips": {"d/A.MOV": {"speaker": ["a"]}}}):
            path = self.base / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(IngestError) as cm:
                rename.plan(str(copy), fields_file=str(path))
            self.assertEqual(cm.exception.code, "FIELDS_INVALID")
        for bad in ("noequals", "Ext=x", "seq=1"):
            with self.assertRaises(IngestError):
                rename.parse_sets([bad])
        with self.assertRaises(IngestError):
            rename.parse_offset("9")
        self.assertIsNotNone(rename.parse_offset("+09:00"))


class MhlPreviousPathTests(Base):
    def test_previous_path_carries_the_history_of_a_file(self):
        copy = self.offloaded({"d/A.MOV": os.urandom(400)})
        self.assertEqual(sorted(mhl.read_history(copy).records), ["d/A.MOV"])
        p, r = self.apply(copy, values={"event": "E"})
        hist = mhl.read_history(copy)
        self.assertEqual(sorted(hist.records), ["d/E_0001.mov"])
        self.assertEqual(len(hist.records["d/E_0001.mov"]), 2, "the first generation's record now belongs to the new path")
        self.assertEqual(hist.records["d/E_0001.mov"][-1].previous_path, "d/A.MOV")


class ContractTests(unittest.TestCase):
    def test_rename_is_experimental_and_additive(self):
        c = contract()
        by_id = {p["id"]: p for p in c["provides"]}
        self.assertEqual(by_id["ingest.rename"]["lifecycle"], "EXPERIMENTAL")
        for op in ("offload", "verify", "inventory"):
            self.assertEqual(by_id[f"ingest.{op}"]["lifecycle"], "STABLE")
        self.assertIn("ingest/rename", {t["tool_id"] for t in c["tools"]})
        for code in ("TEMPLATE_INVALID", "FIELDS_INVALID", "PLAN_CHANGED", "RENAME_REFUSED", "UNDO_INVALID"):
            self.assertEqual(c["errors"][code]["exit_code"], 2)
        self.assertEqual(c["rename"]["default_template"], TEMPLATE)


if __name__ == "__main__":
    unittest.main()
