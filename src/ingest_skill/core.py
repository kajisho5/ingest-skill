"""Offload, verify and inventory. Deterministic, local, no network, no shell.

Guarantees:
- The source is only ever read.
- A destination file is written to a temporary name, fsynced, renamed, then read back and hashed; it counts as
  copied only when that hash equals the hash taken while reading the source.
- An existing destination file is never overwritten: identical content is skipped (resume), different content is
  reported as a conflict and the run fails.
- Symbolic links are not followed; they are listed as skipped.
- Every destination gets ingest-manifest.json (sha256 or xxh64) and an ASC MHL history (ascmhl/), see mhl.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import __version__, mhl
from .errors import IngestError
from .hashing import CHUNK, DEFAULT_HASH, INSTALL_HINT, MHL_FORMAT_FOR, OFFLOAD_HASHES, MultiHash, available, hash_file, require

MANIFEST_NAME = "ingest-manifest.json"
MANIFEST_SCHEMA = "ingest/manifest@1"
# Card and OS housekeeping files: recorded as skipped, never copied.
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", "System Volume Information", "$RECYCLE.BIN"}
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".mkv", ".braw", ".r3d", ".crm", ".mpg", ".mpeg", ".wav", ".bwf", ".mp3", ".aac", ".m4a", ".flac", ".aif", ".aiff"}


@dataclass
class FileRecord:
    path: str                      # relative to the source root, "/" separated
    size: int
    sha256: Optional[str] = None
    mtime: Optional[str] = None
    status: str = "pending"        # copied | skipped_identical | conflict | failed | skipped_junk | skipped_symlink | skipped_ascmhl | skipped_manifest | planned
    detail: str = ""
    hashes: Dict[str, str] = field(default_factory=dict)   # every digest taken: the offload hash and the ASC MHL format(s)


@dataclass
class DestResult:
    root: str
    copied: int = 0
    skipped_identical: int = 0
    conflicts: int = 0
    failed: int = 0
    manifest: Optional[str] = None
    mhl: Optional[str] = None          # the ASC MHL generation written by this run
    mhl_failed: int = 0                # files whose hash differs from an earlier ASC MHL record (action="failed")
    problems: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class OffloadReport:
    source: str
    destinations: List[DestResult]
    files: List[FileRecord] = field(default_factory=list)
    skipped: List[FileRecord] = field(default_factory=list)
    bytes_total: int = 0
    hash: str = DEFAULT_HASH
    dry_run: bool = False
    ok: bool = True
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict:
        return {"schema": "ingest/offload-report@1", "ok": self.ok, "dry_run": self.dry_run, "source": self.source, "hash": self.hash, "bytes_total": self.bytes_total,
                "destinations": [asdict(d) for d in self.destinations], "files": [asdict(f) for f in self.files],
                "skipped": [asdict(f) for f in self.skipped], "started_at": self.started_at, "finished_at": self.finished_at}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    return hash_file(path, ["sha256"])["sha256"]


def _is_junk(rel: Path) -> bool:
    return any(part in JUNK_NAMES or part.startswith("._") for part in rel.parts)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def scan(source: Path) -> Tuple[List[Path], List[FileRecord]]:
    """Regular files under source (sorted, deterministic) and the entries deliberately not copied.
    At the top level, the ASC MHL history folder and ingest-manifest.json belong to the folder, not to its media:
    the history is carried into each destination's own history (offload) and neither is copied as a file."""
    files: List[Path] = []
    skipped: List[FileRecord] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        d = Path(dirpath)
        top = d == source
        for name in sorted(dirnames):
            p = d / name
            if p.is_symlink():
                skipped.append(FileRecord(p.relative_to(source).as_posix(), 0, status="skipped_symlink", detail="symbolic links are not followed"))
            elif top and name == mhl.FOLDER:
                skipped.append(FileRecord(name, 0, status="skipped_ascmhl", detail="ASC MHL history: carried into each destination's ascmhl/ history, not copied as media"))
        dirnames[:] = sorted(n for n in dirnames if not (d / n).is_symlink() and not _is_junk((d / n).relative_to(source)) and not (top and n == mhl.FOLDER))
        for name in sorted(filenames):
            p = d / name
            rel = p.relative_to(source)
            if p.is_symlink():
                skipped.append(FileRecord(rel.as_posix(), 0, status="skipped_symlink", detail="symbolic links are not followed"))
            elif _is_junk(rel):
                skipped.append(FileRecord(rel.as_posix(), p.stat().st_size, status="skipped_junk", detail="card / OS housekeeping file"))
            elif top and name == MANIFEST_NAME:
                skipped.append(FileRecord(rel.as_posix(), p.stat().st_size, status="skipped_manifest", detail="this folder's own ingest manifest; each destination gets its own"))
            elif p.is_file():
                files.append(p)
    return files, skipped


def _copy_verified(src: Path, dst: Path, algorithms: Iterable[str] = ("sha256",)) -> Dict[str, str]:
    """Copy src to dst through a temporary name, hashing what was read; then read dst back and compare every digest.
    Returns the source digests. Raises IngestError on any mismatch; never leaves a partial file at dst."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.ingest-{os.getpid()}.part")
    h = MultiHash(algorithms)
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            for chunk in iter(lambda: fi.read(CHUNK), b""):
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(src, tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    digests = h.digests()
    back = hash_file(dst, algorithms)
    if back != digests:
        dst.unlink()
        bad = ", ".join(f"{a} {back[a]} != {digests[a]}" for a in sorted(digests) if back[a] != digests[a])
        raise IngestError("VERIFY_MISMATCH", f"{dst}: read-back {bad} (source)")
    return digests


def _carry_history(src_hist: "mhl.History", root: Path) -> List[Dict[str, str]]:
    """Copy the source's ASC MHL generations into a destination that has no history yet (verified copies, never
    overwriting), so the new generation continues the source's chain of custody. Returns problems."""
    if not src_hist.entries and not src_hist.problems:
        return []
    if src_hist.problems:
        return [{"code": "MHL_INVALID", "detail": f"the source's ASC MHL history is damaged and was not carried over: {p['detail']}"} for p in src_hist.problems]
    if mhl.has_history(root):
        have = {(e.seq, e.path, e.c4) for e in mhl.read_history(root).entries}
        missing = [e.path for e in src_hist.entries if (e.seq, e.path, e.c4) not in have]
        return [{"code": "MHL_INVALID", "detail": f"{root / mhl.FOLDER} is a different ASC MHL history; the source generations {missing} were not merged"}] if missing else []
    try:
        for name in [e.path for e in src_hist.entries] + [mhl.CHAIN]:
            src, dst = src_hist.folder / name, root / mhl.FOLDER / name
            if dst.exists():
                if hash_file(dst, ["sha256"]) != hash_file(src, ["sha256"]):
                    return [{"code": "MHL_INVALID", "detail": f"{dst} exists with different content; the source history was not carried over"}]
                continue
            _copy_verified(src, dst)
    except (OSError, IngestError) as e:
        return [{"code": "MHL_INVALID", "detail": f"carrying the source's ASC MHL history failed: {e}"}]
    return []


def _mhl_actions(history: "mhl.History", path: str, digests: Dict[str, str]) -> Tuple[str, List[str]]:
    """The ASC MHL action for this generation's hashes of path: original (no earlier record), verified (an earlier
    record matched), failed (an earlier record differs). Also the earlier formats that could not be checked."""
    prior = history.prior(path)
    if not prior:
        return "original", []
    checked = [(fmt, v) for fmt, v, _ in prior if fmt in digests]
    if any(digests[fmt] != v for fmt, v in checked):
        return "failed", []
    unchecked = sorted({fmt for fmt, _, _ in prior if fmt not in digests})
    return ("verified" if checked else "original"), unchecked


def offload(source: str, destinations: Iterable[str], project: Optional[str] = None, dry_run: bool = False, hash_algorithm: str = DEFAULT_HASH) -> OffloadReport:
    src = Path(source).resolve()
    if not src.is_dir():
        raise IngestError("SOURCE_NOT_FOUND", f"source {source} is not a directory")
    if hash_algorithm not in OFFLOAD_HASHES:
        raise IngestError("HASH_UNAVAILABLE", f"unknown hash {hash_algorithm!r}; offload accepts {', '.join(OFFLOAD_HASHES)}")
    require([hash_algorithm, MHL_FORMAT_FOR[hash_algorithm]])
    dests = [Path(d).resolve() for d in destinations]
    if not dests:
        raise IngestError("NO_DESTINATION", "at least one --dest is required")
    if len(set(dests)) != len(dests):
        raise IngestError("DUPLICATE_DESTINATION", "the same destination was given twice; a second copy must be on a different path (ideally a different drive)")
    roots: List[Path] = []
    for d in dests:
        if _inside(d, src) or _inside(src, d):
            raise IngestError("DESTINATION_OVERLAPS_SOURCE", f"{d} and the source {src} contain one another")
        roots.append(d / project / src.name if project else d / src.name)
    files, skipped = scan(src)
    started = datetime.now(timezone.utc)
    report = OffloadReport(source=str(src), destinations=[DestResult(root=str(r)) for r in roots], skipped=skipped, hash=hash_algorithm,
                           dry_run=dry_run, started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"))
    mhl_format = MHL_FORMAT_FOR[hash_algorithm]
    histories: List[mhl.History] = []
    if not dry_run:
        src_hist = mhl.read_history(src)
        for dres, root in zip(report.destinations, roots):
            root.mkdir(parents=True, exist_ok=True)
            dres.problems.extend(_carry_history(src_hist, root))
            histories.append(mhl.read_history(root))
            dres.problems.extend(p for p in histories[-1].problems if p not in dres.problems)
    for f in files:
        rel = f.relative_to(src)
        st = f.stat()
        rec = FileRecord(rel.as_posix(), st.st_size, mtime=datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        report.bytes_total += st.st_size
        if dry_run:
            rec.status = "planned"
            report.files.append(rec)
            continue
        # The offload hash, the ASC MHL format, and every earlier ASC MHL format for this file that can be computed here.
        algorithms = {hash_algorithm, mhl_format} | {fmt for h in histories for fmt, _, _ in h.prior(rec.path) if available(fmt)}
        src_digests: Optional[Dict[str, str]] = None
        statuses = []
        for dres, root in zip(report.destinations, roots):
            dst = root / rel
            try:
                if dst.exists():
                    src_digests = src_digests or hash_file(f, algorithms)
                    if dst.stat().st_size == st.st_size and hash_file(dst, algorithms) == src_digests:
                        dres.skipped_identical += 1
                        statuses.append("skipped_identical")
                    else:
                        dres.conflicts += 1
                        statuses.append("conflict")
                        rec.detail = f"{dst} exists with different content; not overwritten"
                    continue
                digests = _copy_verified(f, dst, algorithms)
                if src_digests is not None and digests != src_digests:
                    raise IngestError("SOURCE_CHANGED", f"{f} changed while it was being copied")
                src_digests = digests
                dres.copied += 1
                statuses.append("copied")
            except (OSError, IngestError) as e:
                dres.failed += 1
                statuses.append("failed")
                rec.detail = str(e)
        rec.hashes = src_digests or {}
        rec.sha256 = rec.hashes.get("sha256")
        rec.status = "failed" if "failed" in statuses else "conflict" if "conflict" in statuses else "copied" if "copied" in statuses else "skipped_identical"
        report.files.append(rec)
    if not dry_run:
        for dres, root, hist in zip(report.destinations, roots, histories):
            dres.manifest = str(write_manifest(root, report))
            _write_mhl(dres, hist, report, started)
    report.ok = all(d.failed == 0 and d.conflicts == 0 and d.mhl_failed == 0 and not d.problems for d in report.destinations)
    report.finished_at = _now()
    return report


def _write_mhl(dres: DestResult, hist: "mhl.History", report: OffloadReport, started: datetime) -> None:
    """Append this run's ASC MHL generation to one destination's history (process "transfer")."""
    if hist.problems:
        dres.warnings.append("no ASC MHL generation written: the destination's ASC MHL history has problems")
        return
    mhl_format = MHL_FORMAT_FOR[report.hash]
    items = []
    for f in report.files:
        if f.status not in ("copied", "skipped_identical") or report.hash not in f.hashes:
            continue
        action, unchecked = _mhl_actions(hist, f.path, f.hashes)
        if action == "failed":
            dres.mhl_failed += 1
            f.detail = (f.detail + "; " if f.detail else "") + f"differs from its earlier ASC MHL record in {dres.root}"
        if unchecked:
            dres.warnings.append(f"{f.path}: earlier {', '.join(unchecked)} record(s) not checked ({INSTALL_HINT})")
        formats = {mhl_format} | {fmt for fmt, _, _ in hist.prior(f.path) if fmt in f.hashes}
        items.append({"path": f.path, "size": f.size, "mtime": f.mtime, "hashes": {fmt: (f.hashes[fmt], action) for fmt in formats}})
    if not items:
        dres.warnings.append("no ASC MHL generation written: no file was copied or verified")
        return
    try:
        path, _ = mhl.write_generation(hist, items, started, __version__)
        dres.mhl = str(path)
    except (OSError, ValueError) as e:
        dres.problems.append({"code": "MHL_INVALID", "detail": f"writing the ASC MHL generation failed: {e}"})


def write_manifest(root: Path, report: OffloadReport) -> Path:
    algo = report.hash
    entries = [{"path": f.path, "size": f.size, algo: f.hashes[algo], "mtime": f.mtime} for f in report.files if algo in f.hashes and f.status in ("copied", "skipped_identical")]
    doc = {"schema": MANIFEST_SCHEMA, "tool": f"ingest-skill {__version__}", "source": report.source, "created_at": _now(), "hash": algo, "files": entries}
    path = root / MANIFEST_NAME
    tmp = root / (MANIFEST_NAME + ".part")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_manifest(target: str) -> Tuple[Path, Dict]:
    p = Path(target)
    path = p / MANIFEST_NAME if p.is_dir() else p
    if not path.is_file():
        raise IngestError("MANIFEST_NOT_FOUND", f"no {MANIFEST_NAME} at {target}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise IngestError("MANIFEST_INVALID", f"{path} is not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("schema") != MANIFEST_SCHEMA:
        raise IngestError("MANIFEST_INVALID", f"{path} is not an {MANIFEST_SCHEMA} document")
    doc.setdefault("hash", "sha256")  # every manifest written before xxh64 existed is sha256
    if doc["hash"] not in OFFLOAD_HASHES or any(doc["hash"] not in e for e in doc.get("files", [])):
        raise IngestError("MANIFEST_INVALID", f"{path}: hash must be one of {', '.join(OFFLOAD_HASHES)} and every file must carry it")
    return path.parent, doc


def verify(target: str) -> Dict:
    """Re-hash every file that ingest-manifest.json and / or the ASC MHL history (ascmhl/) list, in one read per file.
    ok only when nothing is missing or changed and the ASC MHL history is intact. Writes nothing."""
    p = Path(target)
    root = p if p.is_dir() else p.parent
    doc: Optional[Dict] = None
    if not p.is_dir() or (p / MANIFEST_NAME).is_file():
        root, doc = load_manifest(target)
    hist = mhl.read_history(root) if (root / mhl.FOLDER).exists() else None
    if doc is None and hist is None:
        raise IngestError("MANIFEST_NOT_FOUND", f"no {MANIFEST_NAME} and no ASC MHL history ({mhl.FOLDER}/{mhl.CHAIN}) at {target}")
    # path -> list of (algorithm, expected value, against); sizes -> list of (expected size, against)
    expected: Dict[str, List[Tuple[str, str, str]]] = {}
    sizes: Dict[str, List[Tuple[int, str]]] = {}
    if doc is not None:
        require([doc["hash"]])
        for e in doc["files"]:
            expected.setdefault(e["path"], []).append((doc["hash"], e[doc["hash"]], MANIFEST_NAME))
            sizes.setdefault(e["path"], []).append((e["size"], MANIFEST_NAME))
    warnings: List[str] = []
    if hist is not None:
        for path, recs in hist.records.items():
            for r in recs:
                if r.size is not None:
                    sizes.setdefault(path, []).append((r.size, f"{mhl.FOLDER}/{r.generation}"))
                for fmt, (v, action) in r.hashes.items():
                    if action == "failed":
                        continue
                    if available(fmt):
                        expected.setdefault(path, []).append((fmt, v, f"{mhl.FOLDER}/{r.generation}"))
                    else:
                        warnings.append(f"{path}: {fmt} record in {r.generation} not checked ({INSTALL_HINT})")
            if doc is None and path not in expected:
                require([fmt for r in recs for fmt in r.hashes])  # refuse: nothing here can check this file
    out: Dict = {"schema": "ingest/verify-report@1", "root": str(root), "hash": doc["hash"] if doc else None, "checked": 0, "ok_files": 0,
                 "missing": [], "changed": [], "unlisted": [], "failures": [], "warnings": warnings,
                 "mhl": None if hist is None else {"history": str(hist.folder), "generations": len(hist.entries), "problems": hist.problems, "warnings": hist.warnings}}
    for path in sorted(set(expected) | set(sizes)):
        f = root / path
        out["checked"] += 1
        if not f.is_file():
            out["missing"].append(path)
            out["failures"].append({"code": "FILE_MISSING", "path": path})
            continue
        size = f.stat().st_size
        bad = [{"code": "VERIFY_MISMATCH", "path": path, "hash": "size", "expected": s, "actual": size, "against": against} for s, against in sizes.get(path, []) if s != size]
        if not bad:
            exp = expected.get(path, [])
            got = hash_file(f, {a for a, _, _ in exp})
            bad = [{"code": "VERIFY_MISMATCH", "path": path, "hash": a, "expected": v, "actual": got[a], "against": against} for a, v, against in exp if got[a] != v]
        if bad:
            out["changed"].append(path)
            out["failures"].extend(bad)
        else:
            out["ok_files"] += 1
    found, _ = scan(root)
    listed = set(expected) | set(sizes)
    out["unlisted"] = sorted(r for r in (f.relative_to(root).as_posix() for f in found) if r not in listed)
    if hist is not None:
        out["failures"].extend(hist.problems)
    out["ok"] = not out["missing"] and not out["changed"] and not (hist and hist.problems)
    return out


def _ffprobe(path: Path) -> Optional[Dict]:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def clip_facts(path: Path) -> Dict:
    """Measured facts about one clip, or "unknown" when ffprobe is unavailable or cannot read it."""
    probe = _ffprobe(path)
    if probe is None:
        return {"probed": False}
    fmt = probe.get("format") or {}
    v = next((s for s in probe.get("streams") or [] if s.get("codec_type") == "video"), {})
    a = next((s for s in probe.get("streams") or [] if s.get("codec_type") == "audio"), {})
    tags = {**(fmt.get("tags") or {}), **(v.get("tags") or {})}
    dur = fmt.get("duration")
    return {"probed": True, "duration_s": round(float(dur), 3) if dur not in (None, "N/A") else None, "container": fmt.get("format_name"),
            "video_codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"), "frame_rate": v.get("avg_frame_rate"),
            "audio_codec": a.get("codec_name"), "audio_channels": a.get("channels"), "sample_rate": a.get("sample_rate"),
            "timecode": tags.get("timecode"), "creation_time": tags.get("creation_time")}


def inventory(target: str) -> Dict:
    """The clip list of an offloaded folder: every media file with size, its manifest hash and measured facts."""
    root = Path(target).resolve()
    if not root.is_dir():
        raise IngestError("SOURCE_NOT_FOUND", f"{target} is not a directory")
    hashes: Dict[str, str] = {}
    algo: Optional[str] = None
    if (root / MANIFEST_NAME).is_file():
        _, doc = load_manifest(str(root))
        algo = doc["hash"]
        hashes = {e["path"]: e[algo] for e in doc["files"]}
    files, _ = scan(root)
    clips = []
    total = 0.0
    for f in files:
        if f.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        rel = f.relative_to(root).as_posix()
        facts = clip_facts(f)
        total += facts.get("duration_s") or 0.0
        clips.append({"path": rel, "size": f.stat().st_size, "sha256": hashes.get(rel) if algo == "sha256" else None,
                      "hash": {"algorithm": algo, "value": hashes[rel]} if rel in hashes else None, **facts})
    return {"schema": "ingest/inventory@1", "root": str(root), "clips": clips, "clip_count": len(clips), "total_duration_s": round(total, 3),
            "probe": "ffprobe" if shutil.which("ffprobe") else "unavailable (facts are unknown, not zero)", "generated_at": _now()}


def doctor() -> Dict:
    import sys
    ffprobe = shutil.which("ffprobe")
    checks = {"python": {"status": "ok" if sys.version_info >= (3, 9) else "fail", "detail": sys.version.split()[0]},
              "sha256": {"status": "ok" if "sha256" in hashlib.algorithms_available else "fail"},
              "c4": {"status": "ok" if "sha512" in hashlib.algorithms_available else "fail", "detail": "SHA-512 (stdlib): the ASC MHL format recorded with --hash sha256"},
              "xxhash": {"status": "ok" if available("xxh64") else "unavailable",
                         "detail": "xxh64 / xxh3 / xxh128 available" if available("xxh64") else
                         f"optional package not installed: --hash xxh64 is UNAVAILABLE (refused, nothing written) and xxh3 / xxh128 ASC MHL records are not checked; {INSTALL_HINT}"},
              "ffprobe": {"status": "ok" if ffprobe else "degraded", "detail": ffprobe or "not on PATH: inventory facts will be unknown; offload and verify do not need it"}}
    return {"schema": "ingest/doctor@1", "ok": all(c["status"] != "fail" for c in checks.values()), "checks": checks,
            "hash_algorithms": {a: "ok" if available(a) else "unavailable" for a in OFFLOAD_HASHES + ("c4", "md5", "sha1", "xxh3", "xxh128")},
            "version": __version__, "measured_at": _now()}
