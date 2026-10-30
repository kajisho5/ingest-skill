"""Offload, verify and inventory. Deterministic, local, no network, no shell.

Guarantees:
- The source is only ever read.
- A destination file is written to a temporary name, fsynced, renamed, then read back and hashed; it counts as
  copied only when that hash equals the hash taken while reading the source.
- An existing destination file is never overwritten: identical content is skipped (resume), different content is
  reported as a conflict and the run fails.
- Symbolic links are not followed; they are listed as skipped.
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

from . import __version__

MANIFEST_NAME = "ingest-manifest.json"
MANIFEST_SCHEMA = "ingest/manifest@1"
CHUNK = 8 * 1024 * 1024
# Card and OS housekeeping files: recorded as skipped, never copied.
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", "System Volume Information", "$RECYCLE.BIN"}
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".mkv", ".braw", ".r3d", ".crm", ".mpg", ".mpeg", ".wav", ".bwf", ".mp3", ".aac", ".m4a", ".flac", ".aif", ".aiff"}


class IngestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class FileRecord:
    path: str                      # relative to the source root, "/" separated
    size: int
    sha256: Optional[str] = None
    mtime: Optional[str] = None
    status: str = "pending"        # copied | skipped_identical | conflict | failed | skipped_junk | skipped_symlink | planned
    detail: str = ""


@dataclass
class DestResult:
    root: str
    copied: int = 0
    skipped_identical: int = 0
    conflicts: int = 0
    failed: int = 0
    manifest: Optional[str] = None


@dataclass
class OffloadReport:
    source: str
    destinations: List[DestResult]
    files: List[FileRecord] = field(default_factory=list)
    skipped: List[FileRecord] = field(default_factory=list)
    bytes_total: int = 0
    dry_run: bool = False
    ok: bool = True
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict:
        return {"schema": "ingest/offload-report@1", "ok": self.ok, "dry_run": self.dry_run, "source": self.source, "bytes_total": self.bytes_total,
                "destinations": [asdict(d) for d in self.destinations], "files": [asdict(f) for f in self.files],
                "skipped": [asdict(f) for f in self.skipped], "started_at": self.started_at, "finished_at": self.finished_at}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_junk(rel: Path) -> bool:
    return any(part in JUNK_NAMES or part.startswith("._") for part in rel.parts)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def scan(source: Path) -> Tuple[List[Path], List[FileRecord]]:
    """Regular files under source (sorted, deterministic) and the entries deliberately not copied."""
    files: List[Path] = []
    skipped: List[FileRecord] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        d = Path(dirpath)
        for name in sorted(dirnames):
            p = d / name
            if p.is_symlink():
                skipped.append(FileRecord(p.relative_to(source).as_posix(), 0, status="skipped_symlink", detail="symbolic links are not followed"))
        dirnames[:] = sorted(n for n in dirnames if not (d / n).is_symlink() and not _is_junk((d / n).relative_to(source)))
        for name in sorted(filenames):
            p = d / name
            rel = p.relative_to(source)
            if p.is_symlink():
                skipped.append(FileRecord(rel.as_posix(), 0, status="skipped_symlink", detail="symbolic links are not followed"))
            elif _is_junk(rel):
                skipped.append(FileRecord(rel.as_posix(), p.stat().st_size, status="skipped_junk", detail="card / OS housekeeping file"))
            elif p.is_file():
                files.append(p)
    return files, skipped


def _copy_verified(src: Path, dst: Path) -> str:
    """Copy src to dst through a temporary name, hashing what was read; then read dst back and compare.
    Returns the source hash. Raises IngestError on any mismatch; never leaves a partial file at dst."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.ingest-{os.getpid()}.part")
    h = hashlib.sha256()
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
    digest = h.hexdigest()
    back = sha256_file(dst)
    if back != digest:
        dst.unlink()
        raise IngestError("VERIFY_MISMATCH", f"{dst}: read-back sha256 {back} != source {digest}")
    return digest


def offload(source: str, destinations: Iterable[str], project: Optional[str] = None, dry_run: bool = False) -> OffloadReport:
    src = Path(source).resolve()
    if not src.is_dir():
        raise IngestError("SOURCE_NOT_FOUND", f"source {source} is not a directory")
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
    report = OffloadReport(source=str(src), destinations=[DestResult(root=str(r)) for r in roots], skipped=skipped, dry_run=dry_run, started_at=_now())
    for f in files:
        rel = f.relative_to(src)
        st = f.stat()
        rec = FileRecord(rel.as_posix(), st.st_size, mtime=datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        report.bytes_total += st.st_size
        if dry_run:
            rec.status = "planned"
            report.files.append(rec)
            continue
        src_hash: Optional[str] = None
        statuses = []
        for dres, root in zip(report.destinations, roots):
            dst = root / rel
            try:
                if dst.exists():
                    src_hash = src_hash or sha256_file(f)
                    if dst.stat().st_size == st.st_size and sha256_file(dst) == src_hash:
                        dres.skipped_identical += 1
                        statuses.append("skipped_identical")
                    else:
                        dres.conflicts += 1
                        statuses.append("conflict")
                        rec.detail = f"{dst} exists with different content; not overwritten"
                    continue
                digest = _copy_verified(f, dst)
                if src_hash is not None and digest != src_hash:
                    raise IngestError("SOURCE_CHANGED", f"{f} changed while it was being copied")
                src_hash = digest
                dres.copied += 1
                statuses.append("copied")
            except (OSError, IngestError) as e:
                dres.failed += 1
                statuses.append("failed")
                rec.detail = str(e)
        rec.sha256 = src_hash
        rec.status = "failed" if "failed" in statuses else "conflict" if "conflict" in statuses else "copied" if "copied" in statuses else "skipped_identical"
        report.files.append(rec)
    report.ok = all(d.failed == 0 and d.conflicts == 0 for d in report.destinations)
    report.finished_at = _now()
    if not dry_run:
        for dres, root in zip(report.destinations, roots):
            root.mkdir(parents=True, exist_ok=True)
            dres.manifest = str(write_manifest(root, report))
    return report


def write_manifest(root: Path, report: OffloadReport) -> Path:
    entries = [{"path": f.path, "size": f.size, "sha256": f.sha256, "mtime": f.mtime} for f in report.files if f.sha256 and f.status in ("copied", "skipped_identical")]
    doc = {"schema": MANIFEST_SCHEMA, "tool": f"ingest-skill {__version__}", "source": report.source, "created_at": report.finished_at, "hash": "sha256", "files": entries}
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
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != MANIFEST_SCHEMA:
        raise IngestError("MANIFEST_INVALID", f"{path} is not an {MANIFEST_SCHEMA} document")
    return path.parent, doc


def verify(target: str) -> Dict:
    """Re-hash every file a manifest lists. ok only when nothing is missing or changed."""
    root, doc = load_manifest(target)
    out = {"schema": "ingest/verify-report@1", "root": str(root), "checked": 0, "ok_files": 0, "missing": [], "changed": [], "unlisted": []}
    listed = set()
    for e in doc["files"]:
        listed.add(e["path"])
        p = root / e["path"]
        out["checked"] += 1
        if not p.is_file():
            out["missing"].append(e["path"])
        elif p.stat().st_size != e["size"] or sha256_file(p) != e["sha256"]:
            out["changed"].append(e["path"])
        else:
            out["ok_files"] += 1
    found, _ = scan(root)
    out["unlisted"] = sorted(f.relative_to(root).as_posix() for f in found if f.name != MANIFEST_NAME and f.relative_to(root).as_posix() not in listed)
    out["ok"] = not out["missing"] and not out["changed"]
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
    if (root / MANIFEST_NAME).is_file():
        _, doc = load_manifest(str(root))
        hashes = {e["path"]: e["sha256"] for e in doc["files"]}
    files, _ = scan(root)
    clips = []
    total = 0.0
    for f in files:
        if f.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        rel = f.relative_to(root).as_posix()
        facts = clip_facts(f)
        total += facts.get("duration_s") or 0.0
        clips.append({"path": rel, "size": f.stat().st_size, "sha256": hashes.get(rel), **facts})
    return {"schema": "ingest/inventory@1", "root": str(root), "clips": clips, "clip_count": len(clips), "total_duration_s": round(total, 3),
            "probe": "ffprobe" if shutil.which("ffprobe") else "unavailable (facts are unknown, not zero)", "generated_at": _now()}


def doctor() -> Dict:
    import sys
    ffprobe = shutil.which("ffprobe")
    checks = {"python": {"status": "ok" if sys.version_info >= (3, 9) else "fail", "detail": sys.version.split()[0]},
              "sha256": {"status": "ok" if "sha256" in hashlib.algorithms_available else "fail"},
              "ffprobe": {"status": "ok" if ffprobe else "degraded", "detail": ffprobe or "not on PATH: inventory facts will be unknown; offload and verify do not need it"}}
    return {"schema": "ingest/doctor@1", "ok": all(c["status"] != "fail" for c in checks.values()), "checks": checks, "version": __version__, "measured_at": _now()}
