"""ASC MHL (ASC Media Hash List v1.0, XML version 2.0) history: read, check and append.

Layout (ASC MHL v1.0 sections 5.3, 6.3, 7.3): the scope's top level holds an `ascmhl/` folder with one immutable
manifest per generation, `<NNNN>_<folder>_<YYYY-MM-DD>_<HHMMSS>Z.mhl`, and `ascmhl_chain.xml`, which lists every
manifest with its C4 ID. See docs/adr/ADR-001-asc-mhl-and-xxhash64.md for what is and is not implemented.
"""
from __future__ import annotations

import os
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .hashing import MHL_FORMATS, hash_bytes

NS = "urn:ASC:MHL:v2.0"
DIR_NS = "urn:ASC:MHL:DIRECTORY:v2.0"
FOLDER = "ascmhl"
CHAIN = "ascmhl_chain.xml"
VERSION = "2.0"
# The ASC MHL default ignore patterns (.DS_Store and the ascmhl folder), the card housekeeping ingest never copies,
# and this tool's own manifest (rewritten on every run, so it cannot have a stable hash record).
IGNORE_PATTERNS = [".DS_Store", "ascmhl", "ascmhl/", "/ingest-manifest.json", "._*", "Thumbs.db", "desktop.ini",
                   ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", "System Volume Information", "$RECYCLE.BIN"]
_NAME = re.compile(r"^(\d{4,})_.*\.mhl$")


@dataclass
class ChainEntry:
    seq: int
    path: str
    c4: str


@dataclass
class HashRecord:
    path: str
    size: Optional[int]
    hashes: Dict[str, Tuple[str, Optional[str]]]  # format -> (value, action)
    generation: str


@dataclass
class History:
    root: Path
    entries: List[ChainEntry] = field(default_factory=list)
    records: Dict[str, List[HashRecord]] = field(default_factory=dict)
    problems: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def folder(self) -> Path:
        return self.root / FOLDER

    def prior(self, path: str) -> List[Tuple[str, str, str]]:
        """(format, value, generation) of every hash recorded for path that is not labelled failed."""
        return [(fmt, v, r.generation) for r in self.records.get(path, []) for fmt, (v, action) in r.hashes.items() if action != "failed"]


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _parse(data: bytes, what: str) -> ET.Element:
    # An ASC MHL document never needs a DTD; refusing one rules out entity expansion from a file found on a card.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError(f"{what}: a DTD is not allowed in an ASC MHL document")
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"{what}: not well-formed XML ({e})") from None


def parse_manifest(data: bytes, name: str) -> List[HashRecord]:
    root = _parse(data, name)
    if root.tag != _q(NS, "hashlist") or root.get("version") != VERSION:
        raise ValueError(f"{name}: not an ASC MHL {VERSION} hashlist (urn:ASC:MHL:v2.0)")
    out: List[HashRecord] = []
    hashes = root.find(_q(NS, "hashes"))
    for h in [] if hashes is None else hashes.findall(_q(NS, "hash")):
        p = h.find(_q(NS, "path"))
        if p is None or not p.text:
            raise ValueError(f"{name}: a hash element without a path")
        fmts = {c.tag.split("}")[-1]: ((c.text or "").strip(), c.get("action")) for c in h if c.tag.split("}")[-1] in MHL_FORMATS}
        out.append(HashRecord(p.text, int(p.get("size")) if p.get("size") is not None else None, fmts, name))
    return out


def has_history(root: Path) -> bool:
    return (root / FOLDER / CHAIN).is_file()


def read_history(root: Path) -> History:
    """Every generation the chain lists, each checked against its C4 ID. Problems are reported, never raised."""
    h = History(root)
    chain = h.folder / CHAIN
    if not chain.is_file():
        if h.folder.exists():
            h.problems.append({"code": "MHL_INVALID", "detail": f"{h.folder} has no {CHAIN}: not an ASC MHL history"})
        return h
    try:
        doc = _parse(chain.read_bytes(), str(chain))
        if doc.tag != _q(DIR_NS, "ascmhldirectory"):
            raise ValueError(f"{chain}: not an ascmhldirectory (urn:ASC:MHL:DIRECTORY:v2.0)")
        for el in doc.findall(_q(DIR_NS, "hashlist")):
            h.entries.append(ChainEntry(int(el.get("sequencenr", "0")), el.findtext(_q(DIR_NS, "path")) or "",
                                        (el.findtext(_q(DIR_NS, "c4")) or "").strip()))
    except (OSError, ValueError) as e:
        h.problems.append({"code": "MHL_INVALID", "detail": str(e)})
        return h
    if [e.seq for e in h.entries] != list(range(1, len(h.entries) + 1)):
        h.problems.append({"code": "MHL_INVALID", "detail": f"{chain}: sequencenr is not 1, 2, 3, ..."})
    listed = set()
    for e in h.entries:
        p = h.folder / e.path
        listed.add(e.path)
        if not e.path or e.path.startswith("/") or ".." in e.path.split("/"):
            h.problems.append({"code": "MHL_INVALID", "detail": f"{chain}: generation {e.seq} has an unsafe path {e.path!r}"})
            continue
        if not p.is_file():
            h.problems.append({"code": "MHL_INVALID", "detail": f"{p}: listed in {CHAIN} but missing"})
            continue
        data = p.read_bytes()
        actual = hash_bytes(data, "c4")
        if actual != e.c4:
            h.problems.append({"code": "MHL_INVALID", "detail": f"{p}: C4 {actual} != {e.c4} in {CHAIN} (the manifest was altered)"})
            continue
        try:
            for r in parse_manifest(data, e.path):
                h.records.setdefault(r.path, []).append(r)
        except ValueError as err:
            h.problems.append({"code": "MHL_INVALID", "detail": str(err)})
    for p in sorted(h.folder.glob("*.mhl")):
        if p.name not in listed:
            h.warnings.append(f"{p} is not listed in {CHAIN}")
    return h


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.part")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _xml(root: ET.Element, ns: str) -> bytes:
    """Serialize with `ns` as the default namespace (no prefixes, as in the specification's examples). Elements are
    built unqualified and the namespace is declared on the root; ElementTree's default_namespace option would reject
    the unqualified attributes ASC MHL uses."""
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]
    root.set("xmlns", ns)
    ET.indent(root, "  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode").encode("utf-8") + b"\n"


def _dt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def write_generation(history: History, items: List[Dict], started: datetime, tool_version: str, process: str = "transfer") -> Tuple[Path, ChainEntry]:
    """Append one manifest generation and its chain entry. `items`: {path, size, mtime, hashes: {format: (value, action)}};
    `started` is the UTC start of the run (it names the file, section 6.3). The history must have no problems."""
    folder = history.folder
    folder.mkdir(parents=True, exist_ok=True)
    numbers = [e.seq for e in history.entries] + [int(m.group(1)) for m in (_NAME.match(p.name) for p in folder.glob("*.mhl")) if m]
    seq = max(numbers, default=0) + 1
    name = f"{seq:04d}_{history.root.name}_{started.strftime('%Y-%m-%d_%H%M%S')}Z.mhl"
    doc = ET.Element(_q(NS, "hashlist"), {"version": VERSION})
    ci = ET.SubElement(doc, _q(NS, "creatorinfo"))
    ET.SubElement(ci, _q(NS, "creationdate")).text = _dt(started)
    ET.SubElement(ci, _q(NS, "hostname")).text = socket.gethostname() or "unknown"
    ET.SubElement(ci, _q(NS, "tool"), {"version": tool_version}).text = "ingest-skill"
    pi = ET.SubElement(doc, _q(NS, "processinfo"))
    ET.SubElement(pi, _q(NS, "process")).text = process
    ign = ET.SubElement(pi, _q(NS, "ignore"))
    for pat in IGNORE_PATTERNS:
        ET.SubElement(ign, _q(NS, "pattern")).text = pat
    hashes = ET.SubElement(doc, _q(NS, "hashes"))
    for it in sorted(items, key=lambda i: i["path"]):
        h = ET.SubElement(hashes, _q(NS, "hash"))
        attrs = {"size": str(it["size"])}
        if it.get("mtime"):
            attrs["lastmodificationdate"] = it["mtime"]
        ET.SubElement(h, _q(NS, "path"), attrs).text = it["path"]
        for fmt in MHL_FORMATS:
            if fmt in it["hashes"]:
                value, action = it["hashes"][fmt]
                ET.SubElement(h, _q(NS, fmt), {"action": action}).text = value
    data = _xml(doc, NS)
    path = folder / name
    if path.exists():  # manifests are immutable (section 5.2); never replace one
        raise FileExistsError(f"{path} already exists")
    _atomic_write(path, data)
    entry = ChainEntry(seq, name, hash_bytes(data, "c4"))
    chain = folder / CHAIN
    croot = _parse(chain.read_bytes(), str(chain)) if chain.is_file() else ET.Element(_q(DIR_NS, "ascmhldirectory"))
    hl = ET.SubElement(croot, _q(DIR_NS, "hashlist"), {"sequencenr": str(seq)})
    ET.SubElement(hl, _q(DIR_NS, "path")).text = name
    ET.SubElement(hl, _q(DIR_NS, "c4")).text = entry.c4
    _atomic_write(chain, _xml(croot, DIR_NS))
    history.entries.append(entry)
    return path, entry
