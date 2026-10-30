"""Card-structure-aware inventory on tiny synthetic card trees (no real media): one clip per recording, spanned files
grouped in order, camera sidecars, reel / card naming, and offload still copying every file unchanged."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest_skill import cards  # noqa: E402
from ingest_skill.cli import main  # noqa: E402
from ingest_skill.core import MANIFEST_NAME, IngestError, inventory, offload, verify  # noqa: E402

NRT_NS = "urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00"


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def nrt(frames: int = 250, fps: str = "25p", umid: str = "060A2B340101010501010D4313000000AB", serial: str = "0012345") -> str:
    """A NonRealTimeMeta sidecar with the elements MediaInfo and the Adobe XMP Toolkit read."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta xmlns="{NRT_NS}" lastUpdate="2026-10-30T09:00:00+09:00">
  <TargetMaterial umidRef="{umid}"/>
  <Duration value="{frames}"/>
  <LtcChangeTable tcFps="25" halfStep="false"><LtcChange frameCount="0" value="00000010" status="increment"/></LtcChangeTable>
  <CreationDate value="2026-10-30T09:00:00+09:00"/>
  <VideoFormat>
    <VideoRecPort port="DIRECT"/>
    <VideoFrame videoCodec="AVC_3840_2160_HP@L51" captureFps="25p" formatFps="{fps}"/>
    <VideoLayout pixel="3840" numOfVerticalLine="2160" aspectRatio="16:9"/>
  </VideoFormat>
  <AudioFormat numOfChannel="4"><AudioRecPort port="DIRECT" audioCodec="LPCM16" trackDst="CH1"/></AudioFormat>
  <Device manufacturer="Sony" modelName="ILME-FX6V" serialNo="{serial}"/>
  <RecordingMode type="normal" cacheRec="false"/>
</NonRealTimeMeta>
"""


def mediapro(*uris: str) -> str:
    mats = "".join(f'<Material uri="{u}" type="MXF" umid="060A2B3401010105{i:02d}" title="Clip {i}"/>' for i, u in enumerate(uris, 1))
    return f'<?xml version="1.0" encoding="UTF-8"?><MediaProfile xmlns="http://xmlns.sony.net/pro/metadata/mediaprofile" createdAt="2026-10-30T09:00:00+09:00" version="2.00"><Contents>{mats}</Contents></MediaProfile>'


def mpls(items, entry_marks, link_marks=()) -> bytes:
    """A minimal BDMV/AVCHD playlist at the byte layout libbluray's mpls_parse.c reads.
    items: [(clip name, connection_condition)]; marks point at PlayItem indexes."""
    body = b""
    for name, cc in items:
        item = name.encode("ascii") + b"M2TS" + struct.pack(">H", cc & 0x0F) + b"\x00" + struct.pack(">II", 0, 90000)
        body += struct.pack(">H", len(item)) + item
    playlist = struct.pack(">IHHH", 6 + len(body), 0, len(items), 0) + body
    marks = [(1, i) for i in entry_marks] + [(2, i) for i in link_marks]
    mark_block = struct.pack(">IH", 2 + 14 * len(marks), len(marks)) + b"".join(struct.pack(">BBHIHI", 0, t, ref, 0, 0xFFFF, 0) for t, ref in marks)
    list_pos = 40
    mark_pos = list_pos + len(playlist)
    header = b"MPLS0100" + struct.pack(">III", list_pos, mark_pos, 0)
    return header + b"\x00" * (list_pos - len(header)) + playlist + mark_block


def xdroot_card(base: Path) -> Path:
    """Sony XAVC on CFexpress / SxS: XDROOT with MEDIAPRO.XML, Clip/ with M01.XML sidecars and Sub/ proxies."""
    card = base / "FX6_A"
    x = card / "XDROOT"
    write(x / "MEDIAPRO.XML", mediapro("./Clip/A001C001_261030AB.MXF", "./Clip/A001C002_261030CD.MXF"))
    write(x / "DISCMETA.XML", '<?xml version="1.0"?><DiscMeta/>')
    write(x / "CUEUP.XML", '<?xml version="1.0"?><CueUpInfo/>')
    write(x / "Clip" / "A001C001_261030AB.MXF", os.urandom(4000))
    write(x / "Clip" / "A001C001_261030ABM01.XML", nrt(250))
    write(x / "Clip" / "A001C001_261030ABR01.BIM", b"rtmd")
    write(x / "Clip" / "A001C002_261030CD.MXF", os.urandom(3000))
    write(x / "Clip" / "A001C002_261030CDM01.XML", nrt(500, umid="060A2B340101010501010D43130000CD"))
    write(x / "Sub" / "A001C001_261030ABS03.MXF", os.urandom(500))
    write(x / "General" / "Sony" / "Planning" / "plan.xml", "<plan/>")
    write(x / "Take" / ".keep", b"")
    return card


def xavc_s_card(base: Path) -> Path:
    """Sony XAVC S on SD: PRIVATE/M4ROOT with MEDIAPRO.XML, CLIP/ (upper case) with M01.XML sidecars, THMBNL/."""
    card = base / "A7_SD"
    m4 = card / "PRIVATE" / "M4ROOT"
    write(m4 / "MEDIAPRO.XML", mediapro("./CLIP/C0001.MP4"))
    write(m4 / "STATUS.BIN", b"\x00")
    write(m4 / "CLIP" / "C0001.MP4", os.urandom(2000))
    write(m4 / "CLIP" / "C0001M01.XML", nrt(100))
    write(m4 / "CLIP" / "C0002.MP4", os.urandom(1000))   # not in MEDIAPRO.XML and without a sidecar
    write(m4 / "THMBNL" / "C0001T01.JPG", b"jpg")
    write(card / "DCIM" / "100MSDCF" / "DSC00001.JPG", b"jpg")
    return card


def xdcam_ex_card(base: Path) -> Path:
    """XDCAM EX (BPAV): take 709_0001 split into clip folders _01 and _02 (one recording), take 709_0002 in one."""
    card = base / "EX_CARD"
    b = card / "BPAV"
    write(b / "MEDIAPRO.XML", mediapro())
    write(b / "MEDIAPRO.BUP", mediapro())
    for clip, frames in (("709_0001_01", 1000), ("709_0001_02", 400), ("709_0002_01", 50)):
        d = b / "CLPR" / clip
        write(d / f"{clip}.MP4", os.urandom(1500))
        write(d / f"{clip}M01.XML", nrt(frames))
        write(d / f"{clip}.SMI", "<smil/>")
        write(d / f"{clip}R01.BIM", b"bim")
        write(d / f"{clip}I01.PPN", b"ppn")
    write(b / "TAKR" / "709_0001" / "709_0001.SMI", "<smil/>")
    write(b / "TAKR" / "709_0001" / "709_0001M01.XML", "<take/>")
    return card


def avchd_card(base: Path, playlist: bytes) -> Path:
    """AVCHD on SD (8.3 names): PRIVATE/AVCHD/BDMV with STREAM/*.MTS, CLIPINF/*.CPI and PLAYLIST/*.MPL."""
    card = base / "AVCHD_SD"
    bd = card / "PRIVATE" / "AVCHD" / "BDMV"
    write(bd / "INDEX.BDM", b"INDX0100")
    write(bd / "MOVIEOBJ.BDM", b"MOBJ0100")
    write(bd / "PLAYLIST" / "00000.MPL", playlist)
    for n, size in (("00000", 3000), ("00001", 2000), ("00002", 1000)):
        write(bd / "STREAM" / f"{n}.MTS", os.urandom(size))
        write(bd / "CLIPINF" / f"{n}.CPI", b"HDMV0100")
    write(card / "PRIVATE" / "AVCHD" / "AVCHDTN" / "THUMB.TDT", b"tdt")
    return card


def braw_card(base: Path) -> Path:
    card = base / "BMPCC_A001"
    write(card / "A001_10300912_C001.braw", os.urandom(2500))
    write(card / "A001_10300912_C001.sidecar", '{"note": "settings"}')
    write(card / "A001_10300935_C002.braw", os.urandom(1200))
    return card


def generic_card(base: Path) -> Path:
    """A recorder card with no metadata: no index, no sidecar, no reel in the names."""
    card = base / "NO NAME"
    write(card / "DCIM" / "100GOPRO" / "GX010042.MP4", os.urandom(1800))
    write(card / "DCIM" / "100GOPRO" / "GX010042.THM", b"thm")
    write(card / "AUDIO" / "ZOOM0001.WAV", os.urandom(900))
    write(card / "README.txt", "camera B")
    return card


def files_and_hashes(root: Path):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()  # offload reports resolved paths (macOS /private/var, Windows 8.3 names)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def by_id(self, inv):
        return {c["clip_id"]: c for c in inv["clips"]}

    def test_sony_xdroot_one_clip_per_recording_with_sidecar_and_proxy(self):
        inv = inventory(str(xdroot_card(self.tmp)))
        self.assertEqual(inv["schema"], "ingest/inventory@2")
        self.assertEqual(inv["card"]["structures"], [{"id": "sony_xdcam", "root": "XDROOT", "layout": "XDROOT"}])
        self.assertEqual(set(self.by_id(inv)), {"A001C001_261030AB", "A001C002_261030CD"}, "the S03 proxy is not a clip of its own")
        c1 = self.by_id(inv)["A001C001_261030AB"]
        self.assertEqual((c1["structure"], c1["spanned"], [m["path"] for m in c1["members"]]), ("sony_xdcam", False, ["XDROOT/Clip/A001C001_261030AB.MXF"]))
        self.assertEqual(c1["proxies"], ["XDROOT/Sub/A001C001_261030ABS03.MXF"])
        self.assertEqual(c1["related"], ["XDROOT/Clip/A001C001_261030ABR01.BIM"])
        self.assertEqual(c1["sidecars"][0]["path"], "XDROOT/Clip/A001C001_261030ABM01.XML")
        self.assertTrue(c1["sidecars"][0]["parsed"])
        md = c1["metadata"]
        self.assertEqual(md["duration_frames"], 250)
        self.assertEqual(md["video"], {"format_fps": "25p", "codec": "AVC_3840_2160_HP@L51", "width": 3840, "height": 2160, "aspect_ratio": "16:9"})
        self.assertEqual(md["device"], {"manufacturer": "Sony", "model": "ILME-FX6V", "serial": "0012345"})
        self.assertEqual((md["creation_date"], md["audio_codec"], md["ltc_fps"]), ("2026-10-30T09:00:00+09:00", "LPCM16", "25"))
        self.assertEqual(md["mediapro"], {"umid": "060A2B340101010501", "title": "Clip 1"})
        self.assertNotIn("timecode", md, "fields this tool cannot decode reliably are left out, not guessed")
        self.assertIn("XDROOT/MEDIAPRO.XML", inv["card"]["index_files"])
        self.assertEqual(inv["card"]["reel"]["id"], "A001")
        self.assertEqual((inv["card"]["reel"]["source"], inv["card"]["reel"]["scheme"]), ("clip_names", "sony_cam_id_reel"))
        self.assertEqual(inv["warnings"], [])

    def test_sony_xavc_s_m4root_and_unlisted_clip(self):
        inv = inventory(str(xavc_s_card(self.tmp)))
        self.assertEqual(inv["card"]["structures"][0], {"id": "sony_xdcam", "root": "PRIVATE/M4ROOT", "layout": "M4ROOT"})
        clips = self.by_id(inv)
        self.assertEqual(set(clips), {"C0001", "C0002"})
        self.assertEqual(clips["C0001"]["metadata"]["duration_frames"], 100)
        self.assertEqual((clips["C0002"]["sidecars"], clips["C0002"]["metadata"]), ([], {}), "no sidecar: no metadata, nothing invented")
        self.assertIn("PRIVATE/M4ROOT/CLIP/C0002.MP4: not listed in PRIVATE/M4ROOT/MEDIAPRO.XML", inv["warnings"])
        self.assertEqual(inv["card"]["reel"]["source"], "fingerprint", "C0001 carries no reel")

    def test_xdcam_ex_spanned_take_is_one_clip_in_order(self):
        inv = inventory(str(xdcam_ex_card(self.tmp)))
        self.assertEqual(inv["card"]["structures"], [{"id": "sony_xdcam_ex", "root": "BPAV"}])
        self.assertEqual((inv["clip_count"], inv["file_count"], inv["spanned_count"]), (2, 3, 1))
        take = self.by_id(inv)["709_0001"]
        self.assertTrue(take["spanned"])
        self.assertEqual([m["path"] for m in take["members"]], ["BPAV/CLPR/709_0001_01/709_0001_01.MP4", "BPAV/CLPR/709_0001_02/709_0001_02.MP4"])
        self.assertEqual(take["size"], 3000)
        self.assertEqual([s["metadata"]["duration_frames"] for s in take["sidecars"]], [1000, 400])
        self.assertIn("BPAV/TAKR/709_0001/709_0001.SMI", take["related"])
        self.assertIsNone(take["hash"], "a spanned clip has one hash per member, not one for the clip")
        self.assertFalse(self.by_id(inv)["709_0002"]["spanned"])

    def test_avchd_playlist_groups_a_spanned_recording(self):
        # Recording 1 = 00000 + 00001 (no entry mark on 00001, joined seamlessly: connection_condition 6);
        # recording 2 = 00002 (entry mark).
        inv = inventory(str(avchd_card(self.tmp, mpls([("00000", 1), ("00001", 6), ("00002", 1)], entry_marks=[0, 2], link_marks=[1]))))
        self.assertEqual(inv["card"]["structures"], [{"id": "avchd", "root": "PRIVATE/AVCHD/BDMV"}])
        clips = self.by_id(inv)
        self.assertEqual(set(clips), {"00000", "00002"})
        rec = clips["00000"]
        self.assertEqual([m["path"] for m in rec["members"]], ["PRIVATE/AVCHD/BDMV/STREAM/00000.MTS", "PRIVATE/AVCHD/BDMV/STREAM/00001.MTS"])
        self.assertEqual([s["path"] for s in rec["sidecars"]], ["PRIVATE/AVCHD/BDMV/CLIPINF/00000.CPI", "PRIVATE/AVCHD/BDMV/CLIPINF/00001.CPI"])
        self.assertFalse(rec["sidecars"][0]["parsed"], "clip info is listed, not interpreted")
        self.assertEqual((rec["spanned"], rec["size"], clips["00002"]["spanned"]), (True, 5000, False))
        self.assertIn("PRIVATE/AVCHD/BDMV/PLAYLIST/00000.MPL", inv["card"]["index_files"])
        self.assertEqual(inv["warnings"], [])

    def test_avchd_grouping_never_guesses(self):
        cases = {
            "every item has an entry mark": (mpls([("00000", 1), ("00001", 6), ("00002", 1)], entry_marks=[0, 1, 2]), None),
            "a non-seamless item starts a recording": (mpls([("00000", 1), ("00001", 1), ("00002", 1)], entry_marks=[0]), None),
            "no entry marks at all": (mpls([("00000", 1), ("00001", 6), ("00002", 6)], entry_marks=[]), "no entry marks"),
            "not a playlist": (b"garbage", "not read"),
        }
        for name, (playlist, warning) in cases.items():
            with self.subTest(name):
                base = self.tmp / name.replace(" ", "_")
                inv = inventory(str(avchd_card(base, playlist)))
                self.assertEqual((inv["clip_count"], inv["spanned_count"]), (3, 0))
                if warning:
                    self.assertTrue(any(warning in w for w in inv["warnings"]), inv["warnings"])

    def test_avchd_member_on_another_card_is_reported_incomplete(self):
        inv = inventory(str(avchd_card(self.tmp, mpls([("00000", 1), ("00001", 6), ("00002", 6), ("00003", 6)], entry_marks=[0]))))
        rec = self.by_id(inv)["00000"]
        self.assertEqual((len(rec["members"]), rec["incomplete"]), (3, True))
        self.assertTrue(any("00003 not in this folder" in w for w in inv["warnings"]), inv["warnings"])

    def test_braw_sidecar_and_reel_from_the_file_names(self):
        inv = inventory(str(braw_card(self.tmp)))
        self.assertEqual([s["id"] for s in inv["card"]["structures"]], ["blackmagic_raw"])
        c1 = self.by_id(inv)["A001_10300912_C001"]
        self.assertEqual(c1["sidecars"][0]["path"], "A001_10300912_C001.sidecar")
        self.assertEqual((c1["sidecars"][0]["kind"], c1["sidecars"][0]["parsed"], c1["metadata"]), ("blackmagic_raw_sidecar", False, {}))
        self.assertEqual(self.by_id(inv)["A001_10300935_C002"]["sidecars"], [])
        self.assertEqual((inv["card"]["reel"]["id"], inv["card"]["reel"]["scheme"]), ("A001", "blackmagic"))

    def test_card_without_metadata_gets_a_deterministic_fingerprint(self):
        card = generic_card(self.tmp)
        inv = inventory(str(card))
        self.assertEqual([s["id"] for s in inv["card"]["structures"]], ["generic"])
        self.assertEqual(sorted(c["path"] for c in inv["clips"]), ["AUDIO/ZOOM0001.WAV", "DCIM/100GOPRO/GX010042.MP4"])
        self.assertEqual(self.by_id(inv)["DCIM/100GOPRO/GX010042"]["related"], ["DCIM/100GOPRO/GX010042.THM"])
        reel = inv["card"]["reel"]
        self.assertEqual(reel["source"], "fingerprint")
        self.assertRegex(reel["id"], r"^CARD-[0-9A-F]{8}$")
        self.assertEqual(inventory(str(card))["card"]["reel"]["id"], reel["id"], "deterministic")
        write(card / "AUDIO" / "ZOOM0002.WAV", b"another take")
        self.assertNotEqual(inventory(str(card))["card"]["reel"]["id"], reel["id"])

    def test_mixed_reels_are_not_chosen_from(self):
        card = self.tmp / "MIX"
        write(card / "A001_10300912_C001.braw", b"a")
        write(card / "A002_10301012_C001.braw", b"b")
        inv = inventory(str(card))
        self.assertEqual(inv["card"]["reel"]["source"], "fingerprint")
        self.assertTrue(any("more than one reel" in w for w in inv["warnings"]))

    def test_a_sidecar_with_a_dtd_is_not_read(self):
        card = xdroot_card(self.tmp)
        write(card / "XDROOT" / "Clip" / "A001C002_261030CDM01.XML", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><NonRealTimeMeta>&a;</NonRealTimeMeta>')
        c2 = self.by_id(inventory(str(card)))["A001C002_261030CD"]
        self.assertFalse(c2["sidecars"][0]["parsed"])
        self.assertIn("DTD", c2["sidecars"][0]["detail"])
        self.assertEqual(c2["metadata"], {"mediapro": {"umid": "060A2B340101010502", "title": "Clip 2"}})


class OffloadKeepsTheCardWholeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()  # offload reports resolved paths (macOS /private/var, Windows 8.3 names)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_file_of_every_structure_is_copied_byte_for_byte(self):
        sources = [xdroot_card(self.tmp / "a"), xavc_s_card(self.tmp / "b"), xdcam_ex_card(self.tmp / "c"),
                   avchd_card(self.tmp / "d", mpls([("00000", 1), ("00001", 6), ("00002", 1)], entry_marks=[0, 2])), braw_card(self.tmp / "e"), generic_card(self.tmp / "f")]
        for card in sources:
            with self.subTest(card=card.name):
                before = files_and_hashes(card)
                r = offload(str(card), [str(self.tmp / "ssd1" / card.parent.name)])
                self.assertTrue(r.ok, r.to_dict()["destinations"])
                root = Path(r.destinations[0].root)
                after = {k: v for k, v in files_and_hashes(root).items() if k != MANIFEST_NAME and not k.startswith("ascmhl/")}
                self.assertEqual(after, before, "sidecars, index files, playlists and proxies are copied, unchanged and under their own names")
                self.assertEqual(files_and_hashes(card), before, "the card is only read")
                self.assertTrue(verify(str(root))["ok"])
                src_inv, dst_inv = inventory(str(card)), inventory(str(root))
                self.assertEqual([(c["clip_id"], [m["path"] for m in c["members"]]) for c in dst_inv["clips"]],
                                 [(c["clip_id"], [m["path"] for m in c["members"]]) for c in src_inv["clips"]])
                self.assertEqual(dst_inv["card"]["reel"]["id"], src_inv["card"]["reel"]["id"])
                self.assertEqual(dst_inv["card"]["reel"]["source"], "manifest")
                self.assertTrue(all(m["hash"] for c in dst_inv["clips"] for m in c["members"]), "member hashes come from the manifest")

    def test_destination_named_by_reel(self):
        card = xdroot_card(self.tmp)
        r = offload(str(card), [str(self.tmp / "ssd1"), str(self.tmp / "ssd2")], project="2026-10-30_conf", name_by="reel")
        self.assertTrue(r.ok)
        self.assertEqual([Path(d.root).relative_to(self.tmp).as_posix() for d in r.destinations], ["ssd1/2026-10-30_conf/A001", "ssd2/2026-10-30_conf/A001"])
        self.assertEqual(r.to_dict()["card"]["reel"]["source"], "clip_names")
        again = offload(str(card), [str(self.tmp / "ssd1")], project="2026-10-30_conf", name_by="reel")
        self.assertEqual((again.destinations[0].copied, again.destinations[0].skipped_identical), (0, 11), "same name on a re-run: a resume")
        generic = generic_card(self.tmp / "g")
        fp = inventory(str(generic))["card"]["reel"]["id"]
        r = offload(str(generic), [str(self.tmp / "ssd1")], name_by="reel")
        self.assertEqual(Path(r.destinations[0].root).name, fp)

    def test_operator_reel_wins_and_is_kept_by_the_copy(self):
        card = braw_card(self.tmp)
        r = offload(str(card), [str(self.tmp / "ssd1")], name_by="reel", reel="CAM-B_R07")
        root = Path(r.destinations[0].root)
        self.assertEqual(root.name, "CAM-B_R07")
        self.assertEqual(json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))["card"], {"reel": "CAM-B_R07", "reel_source": "operator"})
        inv = inventory(str(root))
        self.assertEqual((inv["card"]["reel"]["id"], inv["card"]["reel"]["source"], inv["card"]["reel"]["recorded_source"]), ("CAM-B_R07", "manifest", "operator"))
        self.assertEqual(inventory(str(root), reel="X1")["card"]["reel"]["id"], "X1")
        self.assertEqual(inventory(str(card))["card"]["reel"]["id"], "A001", "the card itself still says A001")

    def test_an_unsafe_reel_is_refused_before_writing(self):
        card = braw_card(self.tmp)
        for bad in ("../up", "a/b", "", "x" * 65, "-lead"):
            with self.subTest(reel=bad):
                with self.assertRaises(IngestError) as cm:
                    offload(str(card), [str(self.tmp / "ssd1")], name_by="reel", reel=bad if bad else "")
                self.assertEqual(cm.exception.code, "REEL_INVALID")
        self.assertFalse((self.tmp / "ssd1").exists())
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["offload", str(card), "--dest", str(self.tmp / "ssd1"), "--reel", "a/b", "--json"])
        self.assertEqual((code, json.loads(buf.getvalue())["error"]["code"]), (2, "REEL_INVALID"))

    def test_cli_inventory_json(self):
        card = xdcam_ex_card(self.tmp)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["inventory", str(card), "--json"])
        d = json.loads(buf.getvalue())
        self.assertEqual((code, d["clip_count"], d["spanned_count"]), (0, 2, 1))
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["inventory", str(card)])
        self.assertIn("2 clips from 3 files (1 spanned)", buf.getvalue())


class ParserTests(unittest.TestCase):
    def test_mpls_round_trip(self):
        items, marks = cards.parse_mpls(mpls([("00007", 1), ("00008", 5)], entry_marks=[0], link_marks=[1]))
        self.assertEqual(items, [("00007", 1), ("00008", 5)])
        self.assertEqual(marks, [(1, 0), (2, 1)])
        self.assertEqual(cards.recordings_from_playlist(items, marks), [["00007", "00008"]])

    def test_truncated_mpls_is_refused(self):
        data = mpls([("00000", 1), ("00001", 6)], entry_marks=[0])
        for cut in (8, 30, 60, len(data) - 5):
            with self.subTest(cut=cut):
                with self.assertRaises(ValueError):
                    cards.parse_mpls(data[:cut])


if __name__ == "__main__":
    unittest.main(verbosity=2)
