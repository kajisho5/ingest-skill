"""Camera-card structures for the inventory: which files make one recording, what the camera wrote about it, and
which reel / card the folder is. Read-only; standard library only. See docs/adr/ADR-003-card-structure-inventory.md
for the layouts, their sources and the reel precedence.

Recognised layouts (folder names are matched case-insensitively, wherever they sit under the folder):
- sony_xdcam: a folder holding MEDIAPRO.XML and a Clip/ folder (XDROOT, M4ROOT, a Professional Disc root).
  One clip per Clip/<name>.MXF|.MP4; Clip/<name>M01.XML is its NonRealTimeMeta sidecar; Sub/<name>S<nn>.* its proxy.
- sony_xdcam_ex: BPAV/CLPR/<take>_<nn>/ clip folders. The folders of one take are one recording (spanned), in <nn> order.
- avchd: a BDMV/ folder with STREAM/. One clip per STREAM/<n>.MTS, grouped into one recording by the playlists'
  entry marks and seamless connections; CLIPINF/<n>.CPI is listed with it.
- blackmagic_raw: <name>.braw, with <name>.sidecar beside it (listed, not interpreted).
- generic: every other media file, with same-name files beside it listed (not interpreted).

Grouping is reporting only: offload copies every file of the card unchanged, whatever this module says.
"""
from __future__ import annotations

import hashlib
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

MEDIA_EXTENSIONS = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".mkv", ".braw", ".r3d", ".crm", ".mpg", ".mpeg", ".wav", ".bwf", ".mp3", ".aac", ".m4a", ".flac", ".aif", ".aiff"}
XML_LIMIT = 16 * 1024 * 1024      # a camera sidecar or index is a few kB; anything bigger is not read
MPL_LIMIT = 4 * 1024 * 1024
REEL_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Clip names that carry the camera's reel: the first four characters are camera ID / index (A-Z) + reel number (001-999).
#   Sony "Cam ID + Reel#" (A001C001_230101AB) and Panasonic MOV (camera index + card number + C001 + ...)
#   Blackmagic (A001_08151512_C001: camera index, reel number, month, day, hour, minute, clip number)
REEL_PATTERNS = [("sony_cam_id_reel", re.compile(r"^([A-Z]\d{3})[CLR]\d{3}_")), ("blackmagic", re.compile(r"^([A-Z]\d{3})_\d{8}_[CS]\d{3,4}$"))]
_SONY_SIDECAR = re.compile(r"^(?P<stem>.+)M(?P<n>\d\d)\.XML$", re.IGNORECASE)
_SONY_PROXY = re.compile(r"^(?P<stem>.+)S\d\d\.(MXF|MP4)$", re.IGNORECASE)
_EX_CLIP = re.compile(r"^(?P<take>.+)_(?P<n>\d\d)$")
_MPLS_VERSIONS = (b"0100", b"0200", b"0240", b"0300")


@dataclass
class Clip:
    clip_id: str
    structure: str
    members: List[str]                                         # essence files of one recording, in recording order
    sidecars: List[Dict] = field(default_factory=list)         # {"path", "kind", "parsed", "metadata"?, "detail"?}
    proxies: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)           # per-clip files listed, not interpreted
    metadata: Dict = field(default_factory=dict)                # what a sidecar reliably says, keys absent when unknown
    incomplete: bool = False                                    # a playlist names a member that is not in this folder


@dataclass
class CardAnalysis:
    structures: List[Dict] = field(default_factory=list)
    clips: List[Clip] = field(default_factory=list)
    index_files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class _Tree:
    """Root-relative posix paths of the scanned files, with case-insensitive lookups by folder."""

    def __init__(self, rels: Iterable[str]):
        self.files: Dict[str, Dict[str, str]] = {}   # folder (lower) -> {name (lower): rel}
        self.dirs: Dict[str, Dict[str, str]] = {}    # folder (lower) -> {child folder name (lower): child rel}
        self.real: Dict[str, str] = {"": ""}          # folder (lower) -> folder rel
        for rel in rels:
            p = PurePosixPath(rel)
            parent = "" if str(p.parent) == "." else str(p.parent)
            self.files.setdefault(parent.lower(), {})[p.name.lower()] = rel
            parts = PurePosixPath(parent).parts if parent else ()
            for i in range(len(parts)):
                d = "/".join(parts[: i + 1])
                up = "/".join(parts[:i])
                self.real.setdefault(d.lower(), d)
                self.dirs.setdefault(up.lower(), {})[parts[i].lower()] = d

    def child(self, folder: str, name: str) -> Optional[str]:
        return self.dirs.get(folder.lower(), {}).get(name.lower())

    def file(self, folder: str, name: str) -> Optional[str]:
        return self.files.get(folder.lower(), {}).get(name.lower())

    def listing(self, folder: str) -> List[str]:
        return sorted(self.files.get(folder.lower(), {}).values())

    def folders(self) -> List[str]:
        return sorted(self.real.values())

    def under(self, folder: str) -> List[str]:
        low = folder.lower()
        return sorted(rel for d, names in self.files.items() if d == low or d.startswith(low + "/") for rel in names.values())


def _ext(rel: str) -> str:
    return PurePosixPath(rel).suffix.lower()


def _stem(rel: str) -> str:
    return PurePosixPath(rel).stem


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(elem: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in elem if _local(c.tag) == name]


def _kid(elem: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if elem is None:
        return None
    found = _kids(elem, name)
    return found[0] if found else None


def read_xml(path: Path) -> ET.Element:
    """Parse a camera XML file. A DTD is refused (a file found on a card cannot trigger entity expansion), and so is
    a file larger than XML_LIMIT. Raises ValueError (ET.ParseError is one) or OSError."""
    if path.stat().st_size > XML_LIMIT:
        raise ValueError(f"larger than {XML_LIMIT} bytes")
    data = path.read_bytes()
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("a DTD is not allowed in a camera XML file")
    return ET.fromstring(data)


def _attr(elem: Optional[ET.Element], name: str) -> Optional[str]:
    if elem is None:
        return None
    v = elem.get(name)
    return v if v not in (None, "") else None


def _int(v: Optional[str]) -> Optional[int]:
    return int(v) if v is not None and v.isdigit() else None


def parse_nonrealtimemeta(root: ET.Element) -> Dict:
    """The fields of a Sony NonRealTimeMeta (<clip>M01.XML) that MediaInfo and the Adobe XMP Toolkit both read.
    Values are kept as the camera wrote them; a field the file does not carry is absent, never guessed."""
    if _local(root.tag) != "NonRealTimeMeta":
        raise ValueError(f"root element is {_local(root.tag)}, not NonRealTimeMeta")
    out: Dict = {}
    for key, elem, attr in (("creation_date", "CreationDate", "value"), ("last_update", "LastUpdate", "value"),
                            ("umid", "TargetMaterial", "umidRef"), ("title", "Title", "usAscii")):
        v = _attr(_kid(root, elem), attr)
        if v is not None:
            out[key] = v
    frames = _int(_attr(_kid(root, "Duration"), "value"))
    if frames is not None:
        out["duration_frames"] = frames
    ltc_fps = _attr(_kid(root, "LtcChangeTable"), "tcFps")
    if ltc_fps is not None:
        out["ltc_fps"] = ltc_fps
    vf = _kid(root, "VideoFormat")
    frame, layout = _kid(vf, "VideoFrame"), _kid(vf, "VideoLayout")
    video = {k: v for k, v in (("format_fps", _attr(frame, "formatFps")), ("codec", _attr(frame, "videoCodec")),
                               ("width", _int(_attr(layout, "pixel"))), ("height", _int(_attr(layout, "numOfVerticalLine"))),
                               ("aspect_ratio", _attr(layout, "aspectRatio"))) if v is not None}
    if video:
        out["video"] = video
    port = _kid(_kid(root, "AudioFormat"), "AudioRecPort")
    if _attr(port, "audioCodec") is not None:
        out["audio_codec"] = _attr(port, "audioCodec")
    dev = _kid(root, "Device")
    device = {k: v for k, v in (("manufacturer", _attr(dev, "manufacturer")), ("model", _attr(dev, "modelName")),
                                ("serial", _attr(dev, "serialNo"))) if v is not None}
    if device:
        out["device"] = device
    return out


def _sony_sidecar(root: Path, rel: str, parse: bool) -> Dict:
    side: Dict = {"path": rel, "kind": "sony_nonrealtimemeta", "parsed": False}
    if not parse:
        return side
    try:
        side["metadata"] = parse_nonrealtimemeta(read_xml(root / rel))
        side["parsed"] = True
    except (OSError, ValueError) as e:
        side["detail"] = f"not read: {e}"
    return side


def _mediapro(root: Path, rel: str, folder: str, warnings: List[str]) -> Dict[str, Dict]:
    """MEDIAPRO.XML (MediaProfile/Contents/Material): essence path (lower, root-relative) -> {"umid", "title"}."""
    try:
        doc = read_xml(root / rel)
        if _local(doc.tag) != "MediaProfile":
            raise ValueError(f"root element is {_local(doc.tag)}, not MediaProfile")
    except (OSError, ValueError) as e:
        warnings.append(f"{rel}: not read ({e})")
        return {}
    out: Dict[str, Dict] = {}
    for mat in _kids(_kid(doc, "Contents") or ET.Element("x"), "Material"):
        uri = _attr(mat, "uri")
        if not uri:
            continue
        target = PurePosixPath(folder) / uri.replace("\\", "/")
        parts: List[str] = []
        for part in target.parts:
            if part == "..":
                parts = parts[:-1]
            elif part not in (".", ""):
                parts.append(part)
        out["/".join(parts).lower()] = {k: v for k, v in (("umid", _attr(mat, "umid")), ("title", _attr(mat, "title"))) if v}
    return out


def _sony_fam(root: Path, tree: _Tree, folder: str, parse: bool, res: CardAnalysis) -> None:
    clip_dir = tree.child(folder, "Clip")
    sub_dir = tree.child(folder, "Sub")
    layout = PurePosixPath(folder).name.upper() if folder else ""
    res.structures.append({"id": "sony_xdcam", "root": folder or ".", "layout": layout if layout in ("XDROOT", "M4ROOT") else "FAM"})
    res.index_files.extend(tree.listing(folder))
    mediapro_rel = tree.file(folder, "MEDIAPRO.XML")
    listed = _mediapro(root, mediapro_rel, folder, res.warnings) if (parse and mediapro_rel) else {}
    names = tree.listing(clip_dir) if clip_dir else []
    proxies: Dict[str, List[str]] = {}
    for rel in tree.listing(sub_dir) if sub_dir else []:
        m = _SONY_PROXY.match(PurePosixPath(rel).name)
        if m:
            proxies.setdefault(m.group("stem").lower(), []).append(rel)
    for rel in names:
        if _ext(rel) not in (".mxf", ".mp4"):
            continue
        stem = _stem(rel)
        clip = Clip(clip_id=stem, structure="sony_xdcam", members=[rel], proxies=proxies.get(stem.lower(), []))
        for other in names:
            rest = PurePosixPath(other).name[len(stem):]
            if other == rel or not PurePosixPath(other).name.lower().startswith(stem.lower()) or not re.match(r"^[A-Z]\d\d\.", rest, re.IGNORECASE):
                continue
            m = _SONY_SIDECAR.match(PurePosixPath(other).name)
            if m and m.group("stem").lower() == stem.lower() and m.group("n") == "01":
                side = _sony_sidecar(root, other, parse)
                clip.sidecars.append(side)
                clip.metadata = dict(side.get("metadata", {}))
            else:
                clip.related.append(other)
        if listed:
            entry = listed.get(rel.lower())
            if entry is None:
                res.warnings.append(f"{rel}: not listed in {mediapro_rel}")
            else:
                clip.metadata.setdefault("mediapro", entry)
        res.clips.append(clip)


def _sony_ex(root: Path, tree: _Tree, bpav: str, parse: bool, res: CardAnalysis) -> None:
    clpr = tree.child(bpav, "CLPR")
    takr = tree.child(bpav, "TAKR")
    res.structures.append({"id": "sony_xdcam_ex", "root": bpav})
    res.index_files.extend(tree.listing(bpav))
    takes: Dict[str, List[Tuple[int, str]]] = {}
    for name, d in sorted(tree.dirs.get(clpr.lower(), {}).items()) if clpr else []:
        m = _EX_CLIP.match(PurePosixPath(d).name)
        if m:
            takes.setdefault(m.group("take"), []).append((int(m.group("n")), d))
        else:
            res.warnings.append(f"{d}: not a <take>_<nn> clip folder; its files are listed as generic")
    for take, folders in sorted(takes.items()):
        clip = Clip(clip_id=take, structure="sony_xdcam_ex", members=[])
        for _, d in sorted(folders):
            name = PurePosixPath(d).name
            essence = next((r for r in tree.listing(d) if _stem(r).lower() == name.lower() and _ext(r) in (".mp4", ".mxf")), None)
            if essence is None:
                res.warnings.append(f"{d}: no {name}.MP4 essence file")
                continue
            clip.members.append(essence)
            for other in tree.listing(d):
                if other == essence:
                    continue
                m = _SONY_SIDECAR.match(PurePosixPath(other).name)
                if m and m.group("stem").lower() == name.lower() and m.group("n") == "01":
                    side = _sony_sidecar(root, other, parse)
                    clip.sidecars.append(side)
                    if not clip.metadata:
                        clip.metadata = dict(side.get("metadata", {}))
                else:
                    clip.related.append(other)
        take_dir = tree.child(takr, take) if takr else None
        clip.related.extend(tree.listing(take_dir) if take_dir else [])
        if clip.members:
            res.clips.append(clip)


def parse_mpls(data: bytes) -> Tuple[List[Tuple[str, int]], List[Tuple[int, int]]]:
    """PlayItems [(clip name, connection_condition)] and PlayListMarks [(mark_type, play_item_ref)] of a BDMV / AVCHD
    playlist (.MPL / .mpls), at the byte layout libbluray's mpls_parse.c reads. Raises ValueError when malformed."""
    if len(data) < 16 or data[:4] != b"MPLS" or data[4:8] not in _MPLS_VERSIONS:
        raise ValueError("not an MPLS playlist")
    list_pos, mark_pos = struct.unpack(">II", data[8:16])
    try:
        _, n_items = struct.unpack(">HH", data[list_pos + 4: list_pos + 8])
        pos = list_pos + 10
        items: List[Tuple[str, int]] = []
        for _ in range(n_items):
            (length,) = struct.unpack(">H", data[pos: pos + 2])
            start = pos + 2
            if length < 18 or start + length > len(data):
                raise ValueError("PlayItem runs past the end of the file")
            name = data[start: start + 5].decode("ascii")
            if data[start + 5: start + 9] not in (b"M2TS", b"FMTS"):
                raise ValueError(f"PlayItem {name}: unexpected codec identifier")
            (flags,) = struct.unpack(">H", data[start + 9: start + 11])
            items.append((name, flags & 0x0F))
            pos = start + length
        _, n_marks = struct.unpack(">IH", data[mark_pos: mark_pos + 6])
        marks = []
        for i in range(n_marks):
            at = mark_pos + 6 + 14 * i
            if at + 14 > len(data):
                raise ValueError("PlayListMark runs past the end of the file")
            mark_type, ref = struct.unpack(">BH", data[at + 1: at + 4])
            marks.append((mark_type, ref))
    except (struct.error, UnicodeDecodeError) as e:
        raise ValueError(f"malformed playlist: {e}") from None
    return items, marks


def recordings_from_playlist(items: List[Tuple[str, int]], marks: List[Tuple[int, int]]) -> List[List[str]]:
    """One list of clip names per recording. A recording starts at a PlayItem an entry mark (type 1) points to; a
    following PlayItem with no entry mark continues it only when it is joined seamlessly (connection_condition 5 or
    6). Without any entry mark, every PlayItem is its own recording (nothing is grouped on a guess)."""
    entries = {ref for t, ref in marks if t == 1}
    groups: List[List[str]] = []
    for i, (name, cc) in enumerate(items):
        if not groups or not entries or i in entries or cc not in (5, 6):
            groups.append([name])
        else:
            groups[-1].append(name)
    return groups


def _avchd(root: Path, tree: _Tree, bdmv: str, parse: bool, res: CardAnalysis) -> None:
    stream = tree.child(bdmv, "STREAM")
    clipinf = tree.child(bdmv, "CLIPINF")
    playlist = tree.child(bdmv, "PLAYLIST")
    res.structures.append({"id": "avchd", "root": bdmv})
    res.index_files.extend(tree.listing(bdmv) + (tree.listing(playlist) if playlist else []))
    streams = {_stem(r).upper(): r for r in (tree.listing(stream) if stream else []) if _ext(r) in (".mts", ".m2ts")}
    group_of: Dict[str, Tuple[str, ...]] = {}
    conflicts: set = set()
    for rel in tree.listing(playlist) if (playlist and parse) else []:
        if _ext(rel) not in (".mpl", ".mpls"):
            continue
        try:
            if (root / rel).stat().st_size > MPL_LIMIT:
                raise ValueError(f"larger than {MPL_LIMIT} bytes")
            items, marks = parse_mpls((root / rel).read_bytes())
        except (OSError, ValueError) as e:
            res.warnings.append(f"{rel}: not read ({e}); its clips are not grouped from it")
            continue
        if not any(t == 1 for t, _ in marks):
            res.warnings.append(f"{rel}: no entry marks; not used to group recordings")
            continue
        groups = recordings_from_playlist(items, marks)
        for g in groups:
            key = tuple(n.upper() for n in g)
            if len(set(key)) != len(key):
                res.warnings.append(f"{rel}: a recording lists the same clip twice; not grouped")
                continue
            for n in key:
                if group_of.setdefault(n, key) != key:
                    conflicts.add(n)
    for n in sorted(conflicts):
        res.warnings.append(f"{bdmv}/STREAM/{n}: the playlists disagree on its recording; listed on its own")
    seen: set = set()
    for name, rel in sorted(streams.items()):
        if name in seen:
            continue
        key = group_of.get(name)
        if key is None and parse and playlist:
            res.warnings.append(f"{rel}: not referenced by any playlist; listed on its own")
        if key is None or any(n in conflicts for n in key):
            key = (name,)
        if key[0] != name and key[0] in streams:
            continue  # a later member: listed with the recording's first file
        members = [streams[n] for n in key if n in streams]
        clip = Clip(clip_id=name, structure="avchd", members=members, incomplete=len(members) != len(key))
        if clip.incomplete:
            res.warnings.append(f"recording {'+'.join(key)}: {', '.join(n for n in key if n not in streams)} not in this folder (another card?)")
        for n in key:
            seen.add(n)
            info = next((tree.file(clipinf, n + e) for e in (".CPI", ".CLPI") if clipinf and tree.file(clipinf, n + e)), None)
            if info:
                clip.sidecars.append({"path": info, "kind": "avchd_clip_info", "parsed": False})
        res.clips.append(clip)


def analyse(root: Path, rels: List[str], parse: bool = True) -> CardAnalysis:
    """Clips of a folder from its card structure. rels: the root-relative posix paths of the files offload copies
    (core.scan). parse=False reads names only (no sidecar, index or playlist is opened)."""
    tree = _Tree(rels)
    res = CardAnalysis()
    for folder in tree.folders():
        if any(folder.lower() == c.lower() or folder.lower().startswith(c.lower() + "/") for c in [s["root"] for s in res.structures if s["root"] != "."]):
            continue
        name = PurePosixPath(folder).name.upper() if folder else ""
        if name == "BPAV" and tree.child(folder, "CLPR"):
            _sony_ex(root, tree, folder, parse, res)
        elif name == "BDMV" and tree.child(folder, "STREAM"):
            _avchd(root, tree, folder, parse, res)
        elif tree.file(folder, "MEDIAPRO.XML") and tree.child(folder, "Clip"):
            _sony_fam(root, tree, folder, parse, res)
    claimed = set(res.index_files) | {r for c in res.clips for r in c.members + c.proxies + c.related + [s["path"] for s in c.sidecars]}
    for rel in sorted(r for r in rels if r not in claimed and _ext(r) == ".braw"):
        clip = Clip(clip_id=_stem(rel), structure="blackmagic_raw", members=[rel])
        claimed.add(rel)
        parent = str(PurePosixPath(rel).parent)
        side = tree.file("" if parent == "." else parent, _stem(rel) + ".sidecar")
        if side:
            clip.sidecars.append({"path": side, "kind": "blackmagic_raw_sidecar", "parsed": False,
                                  "detail": "settings the camera / Resolve keep beside the clip; not interpreted here"})
            claimed.add(side)
        res.clips.append(clip)
    if any(c.structure == "blackmagic_raw" for c in res.clips):
        res.structures.append({"id": "blackmagic_raw", "root": "."})
    generic = [r for r in sorted(rels) if r not in claimed and _ext(r) in MEDIA_EXTENSIONS]
    for rel in generic:
        parent = str(PurePosixPath(rel).parent)
        siblings = tree.listing("" if parent == "." else parent)
        clip = Clip(clip_id=str(PurePosixPath(rel).with_suffix("")), structure="generic", members=[rel],
                    related=[s for s in siblings if s != rel and _stem(s).lower() == _stem(rel).lower() and _ext(s) not in MEDIA_EXTENSIONS])
        res.clips.append(clip)
    if generic or not res.structures:
        res.structures.append({"id": "generic", "root": "."})
    res.clips.sort(key=lambda c: c.members[0])
    return res


def fingerprint(entries: Iterable[Tuple[str, int]]) -> str:
    """A deterministic card id from the copied files' paths and sizes (not their content): the same on the card and
    on every complete copy of it."""
    h = hashlib.sha256()
    for rel, size in sorted(entries):
        h.update(f"{rel}\t{size}\n".encode("utf-8"))
    return "CARD-" + h.hexdigest()[:8].upper()


def camera_reel(clips: List[Clip]) -> Tuple[Optional[str], Optional[str], List[str]]:
    """(reel, naming scheme, warnings) from the recordings' file names, when every recording carries the same
    camera ID + reel number. Proxies and sidecars are not considered."""
    found: Dict[str, str] = {}
    for c in clips:
        stem = _stem(c.members[0])
        hit = next(((scheme, m.group(1)) for scheme, rx in REEL_PATTERNS for m in [rx.match(stem)] if m), None)
        if hit is None:
            return None, None, []
        found[stem] = hit[1]
        scheme = hit[0]
    reels = sorted(set(found.values()))
    if len(reels) == 1:
        return reels[0], scheme, []
    if len(reels) > 1:
        return None, None, [f"clip names carry more than one reel ({', '.join(reels)}); none chosen"]
    return None, None, []


def derive_reel(clips: List[Clip], entries: Iterable[Tuple[str, int]], operator: Optional[str] = None,
                recorded: Optional[Dict] = None) -> Tuple[Dict, List[str]]:
    """The folder's reel / card id and where it came from, in this precedence:
    1. operator   - given on the command line (--reel)
    2. manifest   - recorded in ingest-manifest.json by the offload that made this copy
    3. clip_names - the camera ID + reel number every recording's name starts with (Sony Cam ID + Reel#, Blackmagic)
    4. fingerprint - CARD-<8 hex> from the files' paths and sizes
    """
    fp = fingerprint(entries)
    if operator:
        return {"id": operator, "source": "operator", "fingerprint": fp}, []
    if recorded and recorded.get("reel"):
        return {"id": recorded["reel"], "source": "manifest", "recorded_source": recorded.get("reel_source"), "fingerprint": fp}, []
    reel, scheme, warnings = camera_reel(clips) if clips else (None, None, [])
    if reel:
        return {"id": reel, "source": "clip_names", "scheme": scheme, "fingerprint": fp}, warnings
    return {"id": fp, "source": "fingerprint", "fingerprint": fp}, warnings
