"""Sync facts per clip and multicam sync-group candidates (ECOSYSTEM_ROADMAP.md step 9, ADR-008). Read-only.

Per clip (`sync` in the inventory): start timecode, frame rate, timecode rate, whether an audio channel carries LTC
(detected from the signal, not decoded), the camera / recorder ids, the wall-clock start and the duration, each with
the source it came from. A fact no source gives is null: unknown, never guessed.

Across cards (`ingest inventory <card> <card> ...`): clips of different cards whose times overlap on a shared basis
(time-of-day timecode, UTC wall clock, or local wall clock) are grouped as a `sync_group` candidate for ffmpeg-skill
`multicam` / `sync`, which measure the real offsets from the audio. A candidate is only a candidate: clocks drift and
can be set wrong.

Sources: Sony NonRealTimeMeta (cards.py), Broadcast Wave `bext` (EBU Tech 3285) and `iXML` chunks (read here with the
standard library), and ffprobe's container tags when ffprobe is installed. LTC is found in PCM WAV files with the
standard library, and in the audio of other files only when ffmpeg is on PATH (decoded to PCM; nothing is written).
"""
from __future__ import annotations

import array
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

AUDIO_ONLY = {".wav", ".bwf", ".mp3", ".aac", ".m4a", ".flac", ".aif", ".aiff"}
LTC_SECONDS = 4.0            # audio analysed per channel for LTC
LTC_MAX_STREAMS = 16
IXML_LIMIT = 1024 * 1024
MIN_OVERLAP_S = 1.0          # two clips must share at least this much time to be grouped
BASES = ("timecode", "utc", "local")
_RATE_NAMES = {"23.98": Fraction(24000, 1001), "23.976": Fraction(24000, 1001), "29.97": Fraction(30000, 1001),
               "47.95": Fraction(48000, 1001), "59.94": Fraction(60000, 1001), "119.88": Fraction(120000, 1001)}


# ---------------------------------------------------------------------------------------------------------- rates
def parse_rate(text: Optional[str]) -> Optional[Fraction]:
    """'25', '25/1', '30000/1001', '29.97', and Sony formatFps ('25p', '59.94i': fields, so 29.97 frames)."""
    if text is None:
        return None
    s = str(text).strip().lower()
    interlaced = s.endswith("i")
    s = s.rstrip("pi")
    try:
        if "/" in s:
            n, d = s.split("/", 1)
            r = Fraction(int(n), int(d))
        else:
            r = _RATE_NAMES.get(s) or Fraction(s).limit_denominator(1001)
    except (ValueError, ZeroDivisionError):
        return None
    if r <= 0:
        return None
    return r / 2 if interlaced else r


def rate_text(r: Optional[Fraction]) -> Optional[str]:
    return None if r is None else f"{r.numerator}/{r.denominator}"


def tc_seconds(tc: str, rate: Fraction) -> Optional[float]:
    """Seconds since midnight of a timecode label HH:MM:SS:FF (';' or '.' before FF: drop-frame, counted back to real
    frames for 29.97 / 59.94)."""
    m = re.match(r"^(\d{1,2}):(\d\d):(\d\d)([:;.,])(\d{1,3})$", tc.strip())
    if not m or rate is None:
        return None
    h, mi, s, sep, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), int(m.group(5))
    nominal = round(rate)
    frames = ((h * 60 + mi) * 60 + s) * nominal + f
    if sep in ";.," and rate.denominator == 1001 and nominal in (30, 60):
        drop = 2 * nominal // 30
        total_min = h * 60 + mi
        frames -= drop * (total_min - total_min // 10)
    return float(frames / rate)


def tc_label(seconds: float, rate: Fraction) -> str:
    """A non-drop timecode label for seconds since midnight (used for BWF time references)."""
    nominal = round(rate)
    frames = int(round(seconds * float(rate)))
    f = frames % nominal
    s = frames // nominal
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def sony_ltc_start(value: Optional[str]) -> Optional[str]:
    """The first LtcChange value of a Sony NonRealTimeMeta, 8 hex digits: the frames, seconds, minutes and hours bytes
    in that order, BCD, with the SMPTE ST 12-1 flag bits masked (bit 6 of the frames byte: drop frame)."""
    if value is None or not re.match(r"^[0-9A-Fa-f]{8}$", value):
        return None
    ff, ss, mm, hh = (int(value[i:i + 2], 16) for i in (0, 2, 4, 6))
    dropframe = bool(ff & 0x40)
    ff, ss, mm, hh = ff & 0x3F, ss & 0x7F, mm & 0x7F, hh & 0x3F

    def bcd(b: int) -> Optional[int]:
        return None if (b & 0x0F) > 9 else (b >> 4) * 10 + (b & 0x0F)
    parts = [bcd(x) for x in (hh, mm, ss, ff)]
    if None in parts or parts[0] > 23 or parts[1] > 59 or parts[2] > 59:
        return None
    return f"{parts[0]:02d}:{parts[1]:02d}:{parts[2]:02d}{';' if dropframe else ':'}{parts[3]:02d}"


# ---------------------------------------------------------------------------------------------------------- WAV
def read_wav(path: Path) -> Dict:
    """The RIFF chunks of a WAV / BWF file that matter for sync: fmt (PCM layout), data (offset, size), bext and iXML.
    Raises ValueError for a file that is not a readable RIFF WAVE file."""
    out: Dict = {}
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a RIFF WAVE file")
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            start = f.tell()
            if cid == b"fmt ":
                fmt = f.read(min(size, 64))
                if len(fmt) < 16:
                    raise ValueError("fmt chunk too short")
                tag, ch, rate, _, align, bits = struct.unpack("<HHIIHH", fmt[:16])
                if tag == 0xFFFE and len(fmt) >= 26:
                    tag = struct.unpack("<H", fmt[24:26])[0]   # WAVE_FORMAT_EXTENSIBLE: the sub-format's first two bytes
                out["fmt"] = {"format": tag, "channels": ch, "rate": rate, "block_align": align, "bits": bits}
            elif cid == b"data":
                out["data"] = (start, size)
            elif cid == b"bext":
                out["bext"] = _bext(f.read(min(size, 602)))
            elif cid.lower() == b"ixml" and size <= IXML_LIMIT:
                out["ixml"] = _ixml(f.read(size))
            f.seek(start + size + (size & 1))
    if "fmt" not in out or "data" not in out:
        raise ValueError("no fmt or data chunk")
    return out


def _text(b: bytes) -> Optional[str]:
    s = b.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
    return s or None


def _bext(b: bytes) -> Dict:
    """EBU Tech 3285 bext: Description 256, Originator 32, OriginatorReference 32, OriginationDate 10, OriginationTime 8,
    TimeReference (uint64 little-endian, samples since midnight)."""
    if len(b) < 346:
        return {}
    out = {k: v for k, v in (("originator", _text(b[256:288])), ("originator_reference", _text(b[288:320])),
                             ("origination_date", _text(b[320:330])), ("origination_time", _text(b[330:338]))) if v}
    out["time_reference"] = struct.unpack("<Q", b[338:346])[0]
    return out


def _ixml(b: bytes) -> Dict:
    """The iXML fields used here: SPEED/TIMECODE_RATE, SPEED/TIMECODE_FLAG, TAPE, PROJECT, and the track names."""
    if b"<!DOCTYPE" in b[:4096].upper() or b"<!ENTITY" in b.upper():
        return {}
    try:
        root = ET.fromstring(b.rstrip(b"\x00 \r\n\t"))
    except ET.ParseError:
        return {}
    def find(path: str) -> Optional[str]:
        e = root.find(path)
        return e.text.strip() if e is not None and e.text and e.text.strip() else None
    out = {k: v for k, v in (("timecode_rate", find("SPEED/TIMECODE_RATE")), ("timecode_flag", find("SPEED/TIMECODE_FLAG")),
                             ("tape", find("TAPE")), ("project", find("PROJECT"))) if v}
    tracks = [t.text.strip() for t in root.findall("TRACK_LIST/TRACK/NAME") if t.text and t.text.strip()]
    if tracks:
        out["track_names"] = tracks
    return out


def wav_channels(path: Path, info: Dict, seconds: float = LTC_SECONDS) -> Optional[List[array.array]]:
    """The first seconds of each channel of a PCM WAV (16 / 24 / 32-bit integer), or None for another encoding."""
    fmt = info["fmt"]
    if fmt["format"] != 1 or fmt["bits"] not in (16, 24, 32) or fmt["channels"] < 1 or fmt["rate"] < 1:
        return None
    width = fmt["bits"] // 8
    frame = width * fmt["channels"]
    start, size = info["data"]
    with open(path, "rb") as f:
        f.seek(start)
        raw = f.read(min(size, int(seconds * fmt["rate"]) * frame))
    raw = raw[: len(raw) - len(raw) % frame]
    if width == 3:   # 24-bit: widen to 32 by placing each sample in the top three bytes
        raw = b"".join(b"\x00" + raw[i:i + 3] for i in range(0, len(raw), 3))
        width = 4
    samples = array.array("h" if width == 2 else "i")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    n = fmt["channels"]
    return [samples[c::n] for c in range(n)]


def wav_duration(info: Dict) -> Optional[float]:
    fmt = info["fmt"]
    if not fmt["block_align"] or not fmt["rate"]:
        return None
    return round(info["data"][1] / fmt["block_align"] / fmt["rate"], 3)


# ---------------------------------------------------------------------------------------------------------- LTC
def detect_ltc(samples: Sequence[int], rate: int) -> bool:
    """True when this channel looks like SMPTE linear timecode: a biphase-mark square wave of 80 bits per frame at 23.976
    to 30 frames per second. Its zero crossings fall at one bit period (a 0 bit) or half of it (a 1 bit), and both
    occur (the sync word holds twelve 1 bits in a row). Nothing is decoded."""
    if rate < 16000 or len(samples) < rate // 2:
        return False
    peak = max(max(samples), -min(samples))
    if peak <= 0:
        return False
    thr = peak * 0.25
    state, last, intervals = 0, None, []
    for i, s in enumerate(samples):
        if s > thr and state <= 0:
            if last is not None:
                intervals.append(i - last)
            state, last = 1, i
        elif s < -thr and state >= 0:
            if last is not None:
                intervals.append(i - last)
            state, last = -1, i
    n = len(intervals)
    per_second = n / (len(samples) / rate)
    if n < 200 or not 1700 <= per_second <= 5200:
        return False
    srt = sorted(intervals)
    p95 = srt[int(n * 0.95)]
    longs = [x for x in srt if x >= 0.75 * p95]
    tb = longs[len(longs) // 2]
    fps = rate / (80 * tb)
    if not 21.0 <= fps <= 33.0:
        return False
    short = sum(1 for x in intervals if 0.3 * tb <= x <= 0.7 * tb)
    long_ = sum(1 for x in intervals if 0.75 * tb <= x <= 1.25 * tb)
    return (short + long_) / n >= 0.9 and short / n >= 0.1 and long_ / n >= 0.1


def _ffmpeg_ltc(path: Path, streams: List[Dict]) -> Dict:
    exe = shutil.which("ffmpeg")
    if not exe:
        return {"checked": False, "channels": [], "detail": "ffmpeg not on PATH: the audio inside this file was not analysed"}
    found = []
    for i, st in enumerate(streams[:LTC_MAX_STREAMS]):
        ch = int(st.get("channels") or 0)
        if ch < 1:
            continue
        try:
            r = subprocess.run([exe, "-v", "error", "-nostdin", "-t", str(LTC_SECONDS), "-i", str(path), "-map", f"0:a:{i}",
                                "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "48000", "-ac", str(ch), "pipe:1"],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return {"checked": False, "channels": [], "detail": f"ffmpeg could not decode the audio: {e}"}
        if r.returncode != 0:
            return {"checked": False, "channels": [], "detail": "ffmpeg could not decode the audio"}
        a = array.array("h")
        a.frombytes(r.stdout[: len(r.stdout) - len(r.stdout) % (2 * ch)])
        if sys.byteorder == "big":
            a.byteswap()
        found += [{"stream": i, "channel": c + 1} for c in range(ch) if detect_ltc(a[c::ch], 48000)]
    return {"checked": True, "channels": found, "method": "ffmpeg decode + biphase-mark detection"}


# ---------------------------------------------------------------------------------------------------------- per clip
def _wall_clock(value: str, source: str) -> Optional[Dict]:
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return {"value": value, "utc": None, "local": dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:23], "source": source}
    utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z"
    local = None if source == "container_creation_time" else dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23]
    return {"value": value, "utc": utc, "local": local, "source": source}


def clip_sync(root: Path, clip: Dict, probe: Optional[Dict]) -> Dict:
    """The sync facts of one inventory clip (its first member, its sidecar metadata, ffprobe's facts when probed)."""
    first = root / clip["path"]
    meta = clip.get("metadata") or {}
    ext = first.suffix.lower()
    kind = "recorder" if ext in AUDIO_ONLY else "camera"
    out: Dict = {"device": {"kind": kind}, "start_timecode": None, "start_tod_s": None, "frame_rate": None, "timecode_rate": None,
                 "wall_clock_start": None, "duration_s": None, "ltc": {"checked": False, "channels": [], "detail": "no readable audio"}}
    src: Dict[str, str] = {}
    wav: Optional[Dict] = None
    if ext in (".wav", ".bwf"):
        try:
            wav = read_wav(first)
        except (OSError, ValueError) as e:
            out["ltc"]["detail"] = f"not a readable WAV file: {e}"
    # frame rate: ffprobe, else the sidecar's format rate
    fr = parse_rate(clip.get("frame_rate")) if clip.get("probed") and clip.get("video_codec") else None
    if fr is not None:
        out["frame_rate"], src["frame_rate"] = rate_text(fr), "ffprobe"
    elif parse_rate((meta.get("video") or {}).get("format_fps")) is not None:
        fr = parse_rate(meta["video"]["format_fps"])
        out["frame_rate"], src["frame_rate"] = rate_text(fr), "sony_nonrealtimemeta"
    # timecode rate and start timecode
    tcr = parse_rate(meta.get("ltc_fps")) if meta.get("ltc_fps") else None
    if tcr is not None:
        src["timecode_rate"] = "sony_nonrealtimemeta"
    if wav and wav.get("ixml", {}).get("timecode_rate") and parse_rate(wav["ixml"]["timecode_rate"]):
        tcr, src["timecode_rate"] = parse_rate(wav["ixml"]["timecode_rate"]), "ixml"
    if tcr is None and fr is not None:
        tcr = fr
        src["timecode_rate"] = src["frame_rate"]
    out["timecode_rate"] = rate_text(tcr)
    if clip.get("timecode"):
        out["start_timecode"], src["start_timecode"] = clip["timecode"], "ffprobe"
    elif sony_ltc_start(meta.get("ltc_start")):
        out["start_timecode"], src["start_timecode"] = sony_ltc_start(meta["ltc_start"]), "sony_nonrealtimemeta"
    if out["start_timecode"] and tcr is not None:
        out["start_tod_s"] = tc_seconds(out["start_timecode"], tcr)
    # A bext time reference of 0 is what recorders write when no timecode is set: read as unknown, not as midnight.
    if wav and wav.get("bext", {}).get("time_reference") and wav["fmt"]["rate"]:
        tod = wav["bext"]["time_reference"] / wav["fmt"]["rate"]
        if tod < 86400:
            out["start_tod_s"], src["start_tod_s"] = round(tod, 6), "bwf_bext_time_reference"
            if not out["start_timecode"] and tcr is not None:
                out["start_timecode"], src["start_timecode"] = tc_label(tod, tcr), "bwf_bext_time_reference"
    elif out["start_tod_s"] is not None:
        src["start_tod_s"] = src["start_timecode"]
    if out["start_tod_s"] is not None:
        out["start_tod_s"] = round(out["start_tod_s"], 6)
    # wall clock: the camera's own record, else the recorder's bext, else the container's creation time
    wc = None
    if meta.get("creation_date"):
        wc = _wall_clock(meta["creation_date"], "sony_nonrealtimemeta")
    if wc is None and wav and wav.get("bext", {}).get("origination_date") and wav["bext"].get("origination_time"):
        wc = _wall_clock(f"{wav['bext']['origination_date']}T{wav['bext']['origination_time'].replace('-', ':').replace('.', ':')}", "bwf_bext_origination")
    if wc is None and clip.get("creation_time"):
        wc = _wall_clock(clip["creation_time"], "container_creation_time")
    out["wall_clock_start"] = wc
    # duration
    if clip.get("duration_s"):
        out["duration_s"], src["duration_s"] = clip["duration_s"], "ffprobe"
    elif meta.get("duration_frames") and fr is not None:
        out["duration_s"], src["duration_s"] = round(meta["duration_frames"] / float(fr), 3), "sony_nonrealtimemeta"
    elif wav and wav_duration(wav):
        out["duration_s"], src["duration_s"] = wav_duration(wav), "wav_data_chunk"
    # device ids
    dev = out["device"]
    for k, v in (meta.get("device") or {}).items():
        dev[k] = v
    if meta.get("device"):
        src["device"] = "sony_nonrealtimemeta"
    if wav:
        for k in ("originator", "originator_reference"):
            if wav.get("bext", {}).get(k):
                dev[k] = wav["bext"][k]
                src["device"] = "bwf_bext"
        if wav.get("ixml", {}).get("tape"):
            dev["tape"] = wav["ixml"]["tape"]
    tags = {}
    if probe:
        tags = {**((probe.get("format") or {}).get("tags") or {})}
        for st in probe.get("streams") or []:
            tags.update(st.get("tags") or {})
        tags = {k.lower(): v for k, v in tags.items()}
    for key, names in (("manufacturer", ("com.apple.quicktime.make", "make")), ("model", ("com.apple.quicktime.model", "model"))):
        if key not in dev:
            v = next((tags[n] for n in names if tags.get(n)), None)
            if v:
                dev[key] = v
                src.setdefault("device", "ffprobe")
    # LTC
    if wav is not None:
        chans = wav_channels(first, wav)
        if chans is None:
            out["ltc"] = {"checked": False, "channels": [], "detail": "not integer PCM: not analysed"}
        else:
            out["ltc"] = {"checked": True, "channels": [{"stream": 0, "channel": c + 1} for c, s in enumerate(chans) if detect_ltc(s, wav["fmt"]["rate"])],
                          "method": "biphase-mark detection on the PCM samples"}
        if wav.get("ixml", {}).get("track_names"):
            out["track_names"] = wav["ixml"]["track_names"]
    elif probe is not None:
        audio = [s for s in probe.get("streams") or [] if s.get("codec_type") == "audio"]
        if audio:
            out["ltc"] = _ffmpeg_ltc(first, audio)
    elif ext not in (".wav", ".bwf") and not clip.get("probed"):
        out["ltc"]["detail"] = "ffprobe not available or cannot read this file: its audio was not analysed"
    out["sources"] = src
    return out


# ---------------------------------------------------------------------------------------------------------- groups
def _interval(s: Dict, basis: str) -> Optional[Tuple[float, float]]:
    dur = s.get("duration_s")
    if not dur:
        return None
    if basis == "timecode":
        start = s.get("start_tod_s")
    else:
        wc = s.get("wall_clock_start") or {}
        v = wc.get(basis)
        if not v:
            return None
        start = (datetime.fromisoformat(v.rstrip("Z")) - datetime(1970, 1, 1)).total_seconds()
    return None if start is None else (start, start + dur)


def sync_groups(cards: List[Dict], min_overlap_s: float = MIN_OVERLAP_S) -> List[Dict]:
    """Clips of different cards (inventories) whose times overlap by at least min_overlap_s on a shared basis, linked
    transitively. Bases in order of preference: time-of-day timecode (jam-synced devices), UTC wall clock, local wall
    clock (the devices' clocks set to the same zone). Each group is a candidate for ffmpeg-skill multicam / sync."""
    clips = [(ci, c) for ci, card in enumerate(cards) for c in card["clips"] if c.get("sync")]
    parent = list(range(len(clips)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(clips)):
        for j in range(i + 1, len(clips)):
            if clips[i][0] == clips[j][0]:
                continue
            for basis in BASES:
                a, b = _interval(clips[i][1]["sync"], basis), _interval(clips[j][1]["sync"], basis)
                if a and b and min(a[1], b[1]) - max(a[0], b[0]) >= min_overlap_s:
                    parent[find(i)] = find(j)
                    break
    comps: Dict[int, List[int]] = {}
    for i in range(len(clips)):
        comps.setdefault(find(i), []).append(i)
    groups = []
    for members in comps.values():
        if len({clips[i][0] for i in members}) < 2:
            continue
        basis = next((b for b in BASES if all(_interval(clips[i][1]["sync"], b) for i in members)), None)
        ivs = {i: _interval(clips[i][1]["sync"], basis) for i in members} if basis else {}
        cams = [i for i in members if clips[i][1]["sync"]["device"]["kind"] == "camera"] or members
        if basis:
            ref = max(cams, key=lambda i: (ivs[i][1] - ivs[i][0], -ivs[i][0]))
            members.sort(key=lambda i: (ivs[i][0], cards[clips[i][0]]["root"], clips[i][1]["path"]))
        else:
            ref = max(cams, key=lambda i: clips[i][1]["sync"]["duration_s"] or 0)
            members.sort(key=lambda i: (cards[clips[i][0]]["root"], clips[i][1]["path"]))
        out_members = []
        for i in members:
            ci, c = clips[i]
            m = {"card": cards[ci]["card"]["reel"]["id"], "root": cards[ci]["root"], "clip_id": c["clip_id"],
                 "path": str(Path(cards[ci]["root"]) / c["path"]), "kind": c["sync"]["device"]["kind"],
                 "duration_s": c["sync"]["duration_s"], "reference": i == ref}
            if basis:
                m["offset_s"] = round(ivs[i][0] - ivs[ref][0], 3)
            out_members.append(m)
        g: Dict = {"basis": basis or "mixed", "members": out_members}
        if basis:
            start, end = min(v[0] for v in ivs.values()), max(v[1] for v in ivs.values())
            common = min(v[1] for v in ivs.values()) - max(v[0] for v in ivs.values())
            g["span_s"] = round(end - start, 3)
            g["common_overlap_s"] = round(max(0.0, common), 3)
            if basis == "timecode":
                g["span_start"] = {"tod_s": round(start, 3)}
            else:
                g["span_start"] = {basis: (datetime(1970, 1, 1) + timedelta(seconds=start)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + ("Z" if basis == "utc" else "")}
        reach = max([abs(m.get("offset_s", 0.0)) for m in out_members] + [0.0])
        refm = next(m for m in out_members if m["reference"])
        others = [m["path"] for m in out_members if not m["reference"]]
        max_offset = int(reach) + 30
        g["next"] = {"skill": "ffmpeg-skill", "tool": "multicam", "script": "scripts/multicam.py",
                     "args": [refm["path"], *others, "--offsets-only", "--max-offset", str(max_offset), "--analyze-seconds", str(max_offset + 90)],
                     "detail": "the clocks only make this a candidate: multicam / sync measure the real offsets from the audio (reference first)"}
        groups.append(g)
    groups.sort(key=lambda g: (g["members"][0]["root"], g["members"][0]["path"]))
    for n, g in enumerate(groups, 1):
        g["id"] = f"SG{n:03d}"
    return [{"id": g.pop("id"), **g} for g in groups]
