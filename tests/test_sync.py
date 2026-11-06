"""Roadmap step 9 / ADR-008: sync facts per clip and multicam sync-group candidates across cards.

The cards are built here: Sony XAVC cards (M01.XML sidecars with creation date, duration, LTC start, device) and a
recorder card holding a Broadcast Wave file (bext + iXML). LTC is synthesised as SMPTE ST 12-1 biphase-mark audio."""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_skill import sync  # noqa: E402
from ingest_skill.core import inventory, inventory_set  # noqa: E402
from test_cards import mediapro, write  # noqa: E402
from test_ingest import run_cli  # noqa: E402

NRT_NS = "urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00"


def nrt(created: str, frames: int, serial: str, ltc: str = None, fps: str = "25p") -> str:
    table = f'<LtcChangeTable tcFps="25" halfStep="false"><LtcChange frameCount="0" value="{ltc}" status="increment"/></LtcChangeTable>' if ltc else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta xmlns="{NRT_NS}">
  <Duration value="{frames}"/>{table}
  <CreationDate value="{created}"/>
  <VideoFormat><VideoFrame videoCodec="AVC_1920_1080_HP@L42" captureFps="{fps}" formatFps="{fps}"/><VideoLayout pixel="1920" numOfVerticalLine="1080" aspectRatio="16:9"/></VideoFormat>
  <Device manufacturer="Sony" modelName="ILME-FX3" serialNo="{serial}"/>
</NonRealTimeMeta>
"""


def camera_card(base: Path, name: str, clips) -> Path:
    """clips: [(clip name, NonRealTimeMeta text)]."""
    m4 = base / name / "PRIVATE" / "M4ROOT"
    write(m4 / "MEDIAPRO.XML", mediapro(*[f"./CLIP/{c}.MP4" for c, _ in clips]))
    for c, meta in clips:
        write(m4 / "CLIP" / f"{c}.MP4", os.urandom(2000))
        write(m4 / "CLIP" / f"{c}M01.XML", meta)
    return base / name


def chunk(cid: bytes, data: bytes) -> bytes:
    return cid + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")


def bwf(path: Path, channels, rate: int, date: str = None, time: str = None, time_reference: int = None, ixml: str = None,
        frames: int = None) -> Path:
    """A 16-bit PCM WAV. channels: lists of samples (equal length), or None with `frames` for silence."""
    fmt = struct.pack("<HHIIHH", 1, len(channels) if channels else 1, rate, rate * 2 * (len(channels) if channels else 1), 2 * (len(channels) if channels else 1), 16)
    body = chunk(b"fmt ", fmt)
    if date:
        bext = (b"interview".ljust(256, b"\x00") + b"Sound Devices".ljust(32, b"\x00") + b"USSDVMIXPRE6II0000000001".ljust(32, b"\x00")
                + date.encode() + time.encode() + struct.pack("<Q", time_reference) + struct.pack("<H", 1) + b"\x00" * 254)
        body += chunk(b"bext", bext)
    if ixml:
        body += chunk(b"iXML", ixml.encode())
    if channels:
        data = struct.pack(f"<{len(channels[0]) * len(channels)}h", *[s for frame in zip(*channels) for s in frame])
    else:
        data = b"\x00\x00" * frames
    body += chunk(b"data", data)
    write(path, b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body)
    return path


def ltc_signal(seconds: float, rate: int, fps: Fraction = Fraction(25), start_frame: int = 0, amp: int = 8000):
    """SMPTE ST 12-1 LTC: 80 bits per frame (BCD time, sync word 0011111111111101), biphase-mark coded."""
    nominal = round(fps)
    bits = []
    for n in range(start_frame, start_frame + int(seconds * float(fps)) + 2):
        f, s = n % nominal, n // nominal
        fields = [(f % 10, 4), (0, 4), (f // 10, 2), (0, 6), (s % 60 % 10, 4), (0, 4), (s % 60 // 10, 3), (0, 5),
                  (s // 60 % 60 % 10, 4), (0, 4), (s // 60 % 60 // 10, 3), (0, 5), (s // 3600 % 10, 4), (0, 4), (s // 3600 // 10, 2), (0, 6)]
        frame_bits = []
        for value, width in fields:
            frame_bits += [(value >> i) & 1 for i in range(width)]
        bits += frame_bits + [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]
    out, level, t = [], 1, 0.0
    bit_len = rate / (80 * float(fps))
    for b in bits:
        level = -level   # a transition at every bit boundary
        half = t + bit_len / 2
        while len(out) < half:
            out.append(level * amp)
        if b:
            level = -level   # and one in the middle of a 1 bit
        t += bit_len
        while len(out) < t:
            out.append(level * amp)
    return out[: int(seconds * rate)]


class SyncGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def shoot(self, with_timecode: bool):
        """Two cameras and a recorder over the same 10 minutes (10:00 local, Japan), plus one later clip on camera A."""
        a = camera_card(self.tmp, "CAM_A", [("C0001", nrt("2026-11-05T10:00:00+09:00", 15000, "1111111", "00000010" if with_timecode else None)),
                                            ("C0002", nrt("2026-11-05T11:00:00+09:00", 2500, "1111111", "00000011" if with_timecode else None))])
        b = camera_card(self.tmp, "CAM_B", [("C0007", nrt("2026-11-05T10:00:02+09:00", 14950, "2222222", "00020010" if with_timecode else None))])
        rate = 1000   # a long, small file: 10 min 2 s of silence at 1 kHz
        ixml = "<BWFXML><PROJECT>Symposium</PROJECT><TAPE>DAY1</TAPE><SPEED><TIMECODE_RATE>25/1</TIMECODE_RATE><TIMECODE_FLAG>NDF</TIMECODE_FLAG></SPEED>" \
               "<TRACK_LIST><TRACK><NAME>MIX</NAME></TRACK></TRACK_LIST></BWFXML>"
        tod = 9 * 3600 + 59 * 60 + 58
        # Without timecode the recorder writes a time reference of 0, which is read as "not set", not as midnight.
        bwf(self.tmp / "REC" / "AUDIO" / "MIXPRE001.WAV", None, rate, "2026-11-05", "09:59:58", tod * rate if with_timecode else 0, ixml if with_timecode else None, frames=602 * rate)
        return [str(a), str(b), str(self.tmp / "REC")]

    def check_group(self, d, basis):
        self.assertEqual(d["schema"], "ingest/inventory-set@1")
        self.assertEqual(len(d["sync_groups"]), 1, d["sync_groups"])
        (g,) = d["sync_groups"]
        self.assertEqual(g["basis"], basis)
        self.assertEqual(len(g["members"]), 3)
        self.assertEqual({m["card"] for m in g["members"]}, {c["card"]["reel"]["id"] for c in d["cards"]}, "one member from each card")
        by = {m["clip_id"]: m for m in g["members"]}
        self.assertEqual(set(by), {"C0001", "C0007", "AUDIO/MIXPRE001"}, "the later clip on camera A is not in the group")
        self.assertTrue(by["C0001"]["reference"], "the longest camera clip is the reference")
        self.assertEqual((by["C0001"]["offset_s"], by["C0007"]["offset_s"], by["AUDIO/MIXPRE001"]["offset_s"]), (0.0, 2.0, -2.0))
        self.assertEqual([m["kind"] for m in sorted(g["members"], key=lambda m: m["clip_id"])], ["recorder", "camera", "camera"])
        self.assertEqual(g["common_overlap_s"], 598.0)
        self.assertEqual((g["next"]["skill"], g["next"]["tool"]), ("ffmpeg-skill", "multicam"))
        self.assertEqual(g["next"]["args"][0], by["C0001"]["path"], "reference first")
        self.assertEqual(set(g["next"]["args"][1:3]), {by["C0007"]["path"], by["AUDIO/MIXPRE001"]["path"]})
        self.assertIn("--offsets-only", g["next"]["args"])

    def test_two_cameras_and_one_recorder_over_ten_minutes_make_one_group_of_three(self):
        d = inventory_set(self.shoot(with_timecode=True))
        self.check_group(d, "timecode")
        cam = next(c for c in d["cards"][0]["clips"] if c["clip_id"] == "C0001")["sync"]
        self.assertEqual((cam["start_timecode"], cam["start_tod_s"], cam["frame_rate"], cam["timecode_rate"], cam["duration_s"]),
                         ("10:00:00:00", 36000.0, "25/1", "25/1", 600.0))
        self.assertEqual(cam["device"], {"kind": "camera", "manufacturer": "Sony", "model": "ILME-FX3", "serial": "1111111"})
        self.assertEqual(cam["wall_clock_start"], {"value": "2026-11-05T10:00:00+09:00", "utc": "2026-11-05T01:00:00.000Z",
                                                   "local": "2026-11-05T10:00:00.000", "source": "sony_nonrealtimemeta"})
        rec = d["cards"][2]["clips"][0]["sync"]
        self.assertEqual((rec["start_timecode"], rec["start_tod_s"], rec["timecode_rate"], rec["frame_rate"]), ("09:59:58:00", 35998.0, "25/1", None))
        self.assertEqual(rec["device"], {"kind": "recorder", "originator": "Sound Devices", "originator_reference": "USSDVMIXPRE6II0000000001", "tape": "DAY1"})
        self.assertEqual(rec["wall_clock_start"]["local"], "2026-11-05T09:59:58.000")
        self.assertIsNone(rec["wall_clock_start"]["utc"], "a bext time has no zone: not turned into UTC")
        self.assertEqual(rec["sources"]["start_tod_s"], "bwf_bext_time_reference")
        self.assertEqual(rec["track_names"], ["MIX"])
        self.assertTrue(rec["ltc"]["checked"])
        self.assertEqual(rec["ltc"]["channels"], [])

    def test_without_timecode_the_local_wall_clocks_group_them(self):
        self.check_group(inventory_set(self.shoot(with_timecode=False)), "local")

    def test_cards_that_do_not_overlap_make_no_group(self):
        a = camera_card(self.tmp, "CAM_A", [("C0001", nrt("2026-11-05T10:00:00+09:00", 1500, "1"))])
        b = camera_card(self.tmp, "CAM_B", [("C0001", nrt("2026-11-05T10:05:00+09:00", 1500, "2"))])
        self.assertEqual(inventory_set([str(a), str(b)])["sync_groups"], [])

    def test_cli_set_and_refusals(self):
        paths = self.shoot(with_timecode=True)
        code, out = run_cli("inventory", *paths, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["sync_groups"]), 1)
        code, out = run_cli("inventory", *paths)
        self.assertIn("SG001", out)
        self.assertEqual(json.loads(run_cli("inventory", paths[0], paths[0], "--json")[1])["error"]["code"], "DUPLICATE_SOURCE")
        self.assertEqual(json.loads(run_cli("inventory", paths[0], paths[1], "--reel", "A001", "--json")[1])["error"]["code"], "REEL_INVALID")
        single = json.loads(run_cli("inventory", paths[0], "--json")[1])
        self.assertEqual(single["schema"], "ingest/inventory@2", "one folder: the inventory as before, with sync facts per clip")
        self.assertIn("sync", single["clips"][0])


class LtcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ltc_found_on_one_channel_of_a_recorder_file(self):
        rate = 48000
        voice = [int(3000 * math.sin(2 * math.pi * 220 * i / rate) + random.randint(-500, 500)) for i in range(2 * rate)]
        bwf(self.tmp / "REC" / "TAKE01.WAV", [voice, ltc_signal(2, rate, start_frame=900000)], rate, "2026-11-05", "10:00:00", 36000 * rate)
        clip = inventory(str(self.tmp / "REC"))["clips"][0]
        self.assertEqual(clip["sync"]["ltc"]["channels"], [{"stream": 0, "channel": 2}])

    def test_detector(self):
        rate = 48000
        for fps in (Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30)):
            with self.subTest(fps=fps):
                self.assertTrue(sync.detect_ltc(ltc_signal(2, rate, fps, start_frame=12345), rate))
        sine = [int(8000 * math.sin(2 * math.pi * 1000 * i / rate)) for i in range(2 * rate)]   # a 1 kHz tone: one interval only
        square = [8000 if (i // 24) % 2 else -8000 for i in range(2 * rate)]
        rng = random.Random(7)
        noise = [rng.randint(-8000, 8000) for _ in range(2 * rate)]
        for name, sig in (("sine", sine), ("square", square), ("noise", noise), ("silence", [0] * rate)):
            with self.subTest(signal=name):
                self.assertFalse(sync.detect_ltc(sig, rate))
        self.assertFalse(sync.detect_ltc(ltc_signal(2, 8000), 8000), "too low a sample rate to tell: not claimed")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe on PATH")
    def test_ltc_inside_a_video_file_with_ffmpeg(self):
        rate = 48000
        wav = bwf(self.tmp / "src.wav", [[0] * (2 * rate), ltc_signal(2, rate)], rate)
        card = self.tmp / "CAM"
        card.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=25:d=2", "-i", str(wav), "-c:v", "libx264", "-c:a", "pcm_s16le",
                        "-timecode", "10:00:00:00", "-shortest", str(card / "C0001.MOV")], check=True)
        s = inventory(str(card))["clips"][0]["sync"]
        self.assertEqual(s["ltc"]["channels"], [{"stream": 0, "channel": 2}])
        self.assertEqual((s["start_timecode"], s["frame_rate"], s["sources"]["start_timecode"]), ("10:00:00:00", "25/1", "ffprobe"))


class FactsTests(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(sync.sony_ltc_start("00000010"), "10:00:00:00")
        self.assertEqual(sync.sony_ltc_start("12345601"), "01:56:34:12")
        self.assertEqual(sync.sony_ltc_start("52000000"), "00:00:00;12", "bit 6 of the frames byte: drop frame")
        self.assertIsNone(sync.sony_ltc_start("0000009A"), "not BCD: unknown")
        self.assertEqual(sync.parse_rate("59.94i"), Fraction(30000, 1001))
        self.assertEqual(sync.parse_rate("29.97p"), Fraction(30000, 1001))
        self.assertEqual(sync.parse_rate("25/1"), Fraction(25))
        self.assertIsNone(sync.parse_rate("0/0"))
        self.assertAlmostEqual(sync.tc_seconds("01:00:00;00", Fraction(30000, 1001)), 3599.9964, places=3)
        self.assertEqual(sync.tc_seconds("10:00:00:12", Fraction(25)), 36000.48)

    def test_unreadable_files_give_unknown_not_zero(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        try:
            write(tmp / "CARD" / "ZOOM0001.WAV", os.urandom(900))
            write(tmp / "CARD" / "GX010042.MP4", os.urandom(900))
            clips = {c["clip_id"]: c["sync"] for c in inventory(str(tmp / "CARD"))["clips"]}
            wav, mp4 = clips["ZOOM0001"], clips["GX010042"]
            self.assertEqual(wav["device"], {"kind": "recorder"})
            self.assertFalse(wav["ltc"]["checked"])
            self.assertIn("not a readable WAV", wav["ltc"]["detail"])
            for s in (wav, mp4):
                self.assertEqual((s["start_timecode"], s["wall_clock_start"], s["duration_s"]), (None, None, None))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
