"""The ecosystem timebase spec's conformance vectors (velisa/timebase@1) against ingest_skill.sync (ADR-013).

tests/fixtures/timebase/timebase-vectors.json is a byte-for-byte copy of Velisa's docs/specs/timebase-vectors.json at
Velisa commit VELISA_COMMIT. This skill reads rates (parse_rate; None is INVALID_RATE), nominal timecode rates, rate
equality, timecode labels both ways (tc_frames, tc_seconds, tc_from_frames, tc_label) and rounds a time to its nearest
frame. Every vector of those functions must pass, the optional p / i suffix included (Sony formatFps uses it). The
others (format_seconds, seek_seconds, transition_frames, join_positions, frames_at `down`, parse_seconds, seconds_of)
are not offered and are counted, so a new vector cannot go unnoticed.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ingest_skill import sync  # noqa: E402
from ingest_skill.sync import TimecodeError  # noqa: E402

VECTORS = ROOT / "tests" / "fixtures" / "timebase" / "timebase-vectors.json"
VELISA_COMMIT = "b7df1d82586f0b7c00fed4074aecac75cbadf893"
VECTORS_SHA256 = "fecd9730470fd79fb0b8dce6a55ef1ac8fd743ea044a1f1541474ebd249e5aa1"
OFFERED = 98


class _Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _rate(value):
    r = sync.parse_rate(value)
    if r is None:
        raise _Refused("INVALID_RATE")
    return r


def _text(v):
    if isinstance(v, Fraction):
        return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"
    return v


def _frames_at(seconds, rate, mode="nearest"):
    if mode != "nearest":
        raise _Refused("INVALID_TIME")
    return sync.round_half_up(Fraction(str(seconds)) * _rate(rate))


def _seconds_to_timecode(seconds, rate, drop_frame):
    return sync.tc_from_frames(_frames_at(seconds, rate), _rate(rate), drop_frame)


FUNCTIONS = {
    "parse_rate": _rate,
    "nominal": lambda rate: sync.nominal_rate(_rate(rate)),
    "same_rate": lambda a, b: _rate(a) == _rate(b),
    "frames_at": _frames_at,
    "timecode_from_frames": lambda frames, rate, drop_frame: sync.tc_from_frames(frames, _rate(rate), drop_frame),
    "frames_from_timecode": lambda label, rate, drop_frame=None: sync.tc_frames(label, _rate(rate), drop_frame),
    "timecode_to_seconds": lambda label, rate, drop_frame=None: sync.tc_seconds(label, _rate(rate), drop_frame),
    "seconds_to_timecode": _seconds_to_timecode,
}


def _offered(vec):
    if vec["fn"] not in FUNCTIONS:
        return False
    return not (vec["fn"] == "frames_at" and vec["args"].get("mode") == "down")


class TimebaseVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = VECTORS.read_bytes()
        cls.doc = json.loads(cls.raw.decode("utf-8"))

    def test_the_copy_is_velisas_file(self):
        self.assertEqual(self.doc["spec"], "velisa/timebase@1")
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), VECTORS_SHA256,
                         f"the vectors are a copy of Velisa {VELISA_COMMIT}; re-copy them with that commit recorded")

    def test_every_offered_vector_passes(self):
        ran = 0
        for vec in self.doc["vectors"]:
            if not _offered(vec):
                continue
            ran += 1
            with self.subTest(vec["id"]):
                try:
                    got = _text(FUNCTIONS[vec["fn"]](**vec["args"]))
                except (TimecodeError, _Refused) as e:
                    self.assertEqual(e.code, vec.get("error"), f"{vec['id']}: {e}")
                    continue
                self.assertNotIn("error", vec, f"{vec['id']}: gave {got!r}")
                self.assertEqual(got, vec["expect"], vec["id"])
        self.assertEqual(ran, OFFERED)
        self.assertEqual(len(self.doc["vectors"]) - ran, 36)

    def test_the_issues_cases(self):
        ntsc = Fraction(30000, 1001)
        for label, code in (("00:01:00;00", "NONEXISTENT_LABEL"), ("00:01:00;01", "NONEXISTENT_LABEL"),
                            ("00:00:00:30", "INVALID_TIMECODE"), ("24:00:00:00", "INVALID_TIMECODE")):
            with self.assertRaises(TimecodeError) as cm:
                sync.tc_seconds(label, ntsc)
            self.assertEqual(cm.exception.code, code, label)
        with self.assertRaises(TimecodeError) as cm:
            sync.tc_seconds("00:00:01;00", Fraction(25))
        self.assertEqual(cm.exception.code, "DROP_FRAME_UNSUPPORTED")
        self.assertEqual(sync.parse_rate("29.970"), ntsc)  # was 2997/100
        self.assertEqual(sync.parse_rate("47.952"), Fraction(48000, 1001))  # was 5994/125
        self.assertIsNone(sync.parse_rate("1001"))  # above 1000
        self.assertEqual(sync.tc_label(86399.99, Fraction(25)), None)  # rounds to 24:00:00:00: refused, not 00:00:00:00
        self.assertEqual(sync.tc_label(36000.02, Fraction(25)), "10:00:00:01")  # half a frame goes up

    def test_a_refused_start_timecode_gives_no_time_of_day_and_says_why(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "C0001.MP4").write_bytes(b"\0" * 16)
        clip = {"path": "C0001.MP4", "probed": True, "video_codec": "h264", "frame_rate": "30000/1001",
                "timecode": "00:01:00;00"}
        s = sync.clip_sync(tmp, clip, None)
        self.assertEqual(s["start_timecode"], "00:01:00;00")  # the fact as the file states it
        self.assertIsNone(s["start_tod_s"])
        self.assertEqual(s["start_timecode_error"]["code"], "NONEXISTENT_LABEL")
        clip["timecode"] = "00:01:00;02"
        s = sync.clip_sync(tmp, clip, None)
        self.assertEqual(s["start_tod_s"], round(1800 * 1001 / 30000, 6))
        self.assertNotIn("start_timecode_error", s)


if __name__ == "__main__":
    unittest.main()
