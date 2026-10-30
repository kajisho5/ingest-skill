"""Offload, verify and inventory. Deterministic, local, no network, no shell.

Guarantees:
- The source is only ever read.
- The source is read once per file, whatever the number of destinations: each chunk is hashed and handed to one
  writer thread per destination (ADR-004). A write error ends only that destination's copy of the file.
- A destination file is written to a temporary name, fsynced, renamed, then read back and hashed; it counts as
  copied only when that hash equals the hash taken while reading the source.
- An existing destination file is never overwritten: identical content is skipped (resume), different content is
  reported as a conflict and the run fails.
- Symbolic links are not followed; they are listed as skipped.
- Every destination gets ingest-manifest.json (sha256 or xxh64) and an ASC MHL history (ascmhl/), see mhl.py.
- The card structure (cards.py) is reporting only: it names the reel and groups the inventory, it never decides
  which file is copied or what it is called inside the card folder.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from . import __version__, cards, mhl
from .errors import IngestError
from .hashing import CHUNK, DEFAULT_HASH, INSTALL_HINT, MHL_FORMAT_FOR, OFFLOAD_HASHES, MultiHash, available, hash_file, require

MANIFEST_NAME = "ingest-manifest.json"
MANIFEST_SCHEMA = "ingest/manifest@1"
# Card and OS housekeeping files: recorded as skipped, never copied.
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", "System Volume Information", "$RECYCLE.BIN"}
MEDIA_EXTENSIONS = cards.MEDIA_EXTENSIONS
NAME_BY = ("folder", "reel")


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
    card: Dict = field(default_factory=dict)   # {"reel": {...}, "name_by": "folder" | "reel"}

    def to_dict(self) -> Dict:
        return {"schema": "ingest/offload-report@1", "ok": self.ok, "dry_run": self.dry_run, "source": self.source, "hash": self.hash, "bytes_total": self.bytes_total,
                "card": self.card,
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


# Chunks a destination may lag behind the source read before the read waits for it (ADR-004). The chunks are shared,
# not copied, so at most about (QUEUE_DEPTH + 2) x CHUNK bytes are held, whatever the number of destinations.
QUEUE_DEPTH = 4
_END, _ABORT = object(), object()


class _Writer(threading.Thread):
    """One destination of a fan-out copy: writes the chunks it is given to a temporary name, then fsyncs and renames.
    A write error ends this destination only: it keeps taking chunks (so the source read never waits on it), removes
    its temporary file and keeps the error. Nothing is left at dst unless every chunk was written and synced."""

    def __init__(self, src: Path, dst: Path):
        super().__init__(name=f"ingest-write {dst}", daemon=True)
        self.src, self.dst = src, dst
        self.tmp = dst.with_name(f".{dst.name}.ingest-{os.getpid()}.part")
        self.chunks: "queue.Queue" = queue.Queue(maxsize=QUEUE_DEPTH)
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        fo = None
        item = None
        try:
            try:
                self.dst.parent.mkdir(parents=True, exist_ok=True)
                fo = open(self.tmp, "wb")
            except OSError as e:
                self.error = e
            while True:
                item = self.chunks.get()
                if item is _END or item is _ABORT:
                    break
                if self.error is None:
                    try:
                        fo.write(item)
                    except OSError as e:
                        self.error = e
            if self.error is None and item is _ABORT:
                self.error = IngestError("INCOMPLETE", f"{self.dst}: not written, the source read did not complete")
            if self.error is None:
                fo.flush()
                os.fsync(fo.fileno())
                fo.close()
                fo = None
                shutil.copystat(self.src, self.tmp)
                os.replace(self.tmp, self.dst)
        except Exception as e:  # noqa: BLE001 - any failure is this destination's failure, never the others'
            self.error = self.error or e
        finally:
            if fo is not None:
                try:
                    fo.close()
                except OSError:
                    pass
            try:
                if self.tmp.exists():
                    self.tmp.unlink()
            except OSError:
                pass


def _give(w: _Writer, item: object) -> None:
    """Hand one item to a writer, waiting while its queue is full (backpressure), but never on a writer that died."""
    while True:
        try:
            w.chunks.put(item, timeout=1.0)
            return
        except queue.Full:
            if not w.is_alive():
                return


def _fan_out(src: Path, dsts: Sequence[Path], algorithms: Iterable[str]) -> Tuple[Union[Dict[str, str], BaseException], List[Optional[BaseException]]]:
    """Read src ONCE, hashing every chunk and handing it to one writer per destination (temporary name, fsync, rename).
    With no destination this is a plain hash of src. Returns the source digests (or the error that stopped the read)
    and, per destination, None when the file was written and renamed into place, else the error. No read-back here.

    The source is opened once and read once. If it is not read to the end, or its size or modification time changed
    while it was read (SOURCE_CHANGED), no destination keeps the file."""
    writers = [_Writer(src, d) for d in dsts]
    for w in writers:
        w.start()
    h = MultiHash(algorithms)
    result: Union[Dict[str, str], BaseException]
    end = _ABORT
    try:
        before = os.stat(src)
        read = 0
        with open(src, "rb") as fi:
            for chunk in iter(lambda: fi.read(CHUNK), b""):
                for w in writers:
                    if w.error is None:   # a failed destination is skipped; it drains whatever it was already given
                        _give(w, chunk)
                h.update(chunk)
                read += len(chunk)
        after = os.stat(src)
        if read != before.st_size or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            result = IngestError("SOURCE_CHANGED", f"{src} changed while it was being copied")
        else:
            result = h.digests()
            end = _END
    except Exception as e:  # noqa: BLE001 - the source could not be read: every destination of this file fails
        result = e
    finally:
        for w in writers:
            _give(w, end)   # always delivered: a writer keeps taking chunks until it sees the end
        for w in writers:
            w.join()
    errors: List[Optional[BaseException]] = [w.error if end is _END else (w.error or result) for w in writers]
    return result, errors


def _hash_each(paths: Sequence[Path], algorithms: Iterable[str]) -> List[Union[Dict[str, str], BaseException]]:
    """hash_file of every path, one thread per path (destinations are usually separate drives): the read-backs."""
    algorithms = list(algorithms)

    def one(p: Path) -> Union[Dict[str, str], BaseException]:
        try:
            return hash_file(p, algorithms)
        except Exception as e:  # noqa: BLE001
            return e

    if len(paths) <= 1:
        return [one(p) for p in paths]
    with ThreadPoolExecutor(max_workers=len(paths), thread_name_prefix="ingest-readback") as pool:
        return list(pool.map(one, paths))


def _mismatch(dst: Path, back: Dict[str, str], digests: Dict[str, str]) -> IngestError:
    bad = ", ".join(f"{a} {back[a]} != {digests[a]}" for a in sorted(digests) if back[a] != digests[a])
    return IngestError("VERIFY_MISMATCH", f"{dst}: read-back {bad} (source)")


def _copy_verified(src: Path, dst: Path, algorithms: Iterable[str] = ("sha256",)) -> Dict[str, str]:
    """Copy src to one dst through a temporary name, hashing what was read; then read dst back and compare every
    digest. Returns the source digests. Raises on any failure or mismatch; never leaves a partial file at dst."""
    algorithms = list(algorithms)
    digests, (err,) = _fan_out(src, [dst], algorithms)
    if isinstance(digests, BaseException):
        raise digests
    if err is not None:
        raise err
    back = hash_file(dst, algorithms)
    if back != digests:
        dst.unlink()
        raise _mismatch(dst, back, digests)
    return digests


def _offload_file(src: Path, size: int, dsts: Sequence[Path], algorithms: Iterable[str]) -> Tuple[Optional[Dict[str, str]], List[Tuple[str, str]]]:
    """One source file to every destination: one read of the source (written to every destination that does not have
    the file yet), then a read-back of every destination, in parallel. Returns the source digests (None when the source
    could not be read) and, per destination, (status, detail): copied, skipped_identical, conflict or failed."""
    algorithms = list(algorithms)
    existing = [d.exists() for d in dsts]
    to_write = [d for d, e in zip(dsts, existing) if not e]
    digests, write_errors = _fan_out(src, to_write, algorithms)
    results: List[Optional[Tuple[str, str]]] = [None] * len(dsts)
    readback: List[int] = []
    werr = iter(write_errors)
    for i, (d, e) in enumerate(zip(dsts, existing)):
        if isinstance(digests, BaseException):
            results[i] = ("failed", str(digests))
            if not e:
                next(werr)
        elif e:
            try:
                same_size = d.stat().st_size == size
            except OSError as exc:
                results[i] = ("failed", str(exc))
                continue
            if same_size:
                readback.append(i)
            else:
                results[i] = ("conflict", f"{d} exists with different content; not overwritten")
        else:
            err = next(werr)
            if err is not None:
                results[i] = ("failed", str(err) if str(err).startswith(str(d)) else f"{d}: {err}")
            else:
                readback.append(i)
    if isinstance(digests, BaseException):
        return None, [r for r in results if r is not None]
    for i, back in zip(readback, _hash_each([dsts[i] for i in readback], algorithms)):
        d = dsts[i]
        if isinstance(back, BaseException):
            results[i] = ("failed", str(back))
        elif existing[i]:
            results[i] = ("skipped_identical", "") if back == digests else ("conflict", f"{d} exists with different content; not overwritten")
        elif back == digests:
            results[i] = ("copied", "")
        else:
            try:
                d.unlink()
            except OSError:
                pass
            results[i] = ("failed", str(_mismatch(d, back, digests)))
    return digests, [r for r in results if r is not None]


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


def check_reel(reel: Optional[str]) -> Optional[str]:
    """An operator-given reel / card id must be usable as one folder name on every OS."""
    if reel is not None and not cards.REEL_SAFE.match(reel):
        raise IngestError("REEL_INVALID", f"reel {reel!r}: use 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit")
    return reel


def card_reel(src: Path, files: List[Path], reel: Optional[str] = None, recorded: Optional[Dict] = None) -> Tuple[Dict, List[str]]:
    """The reel of a card folder from file names and sizes only (no file is opened), see cards.derive_reel."""
    rels = [f.relative_to(src).as_posix() for f in files]
    analysis = cards.analyse(src, rels, parse=False)
    return cards.derive_reel(analysis.clips, [(r, f.stat().st_size) for r, f in zip(rels, files)], operator=reel, recorded=recorded)


def offload(source: str, destinations: Iterable[str], project: Optional[str] = None, dry_run: bool = False, hash_algorithm: str = DEFAULT_HASH,
            name_by: str = "folder", reel: Optional[str] = None) -> OffloadReport:
    """Copy the card folder to each destination as <dest>/<project>/<name>, where name is the source folder's name
    (name_by="folder", the default) or its reel / card id (name_by="reel", see cards.derive_reel). Every file of the
    folder is copied unchanged, whatever its card structure."""
    src = Path(source).resolve()
    if not src.is_dir():
        raise IngestError("SOURCE_NOT_FOUND", f"source {source} is not a directory")
    if name_by not in NAME_BY:
        raise IngestError("REEL_INVALID", f"name_by must be one of {', '.join(NAME_BY)}")
    check_reel(reel)
    if hash_algorithm not in OFFLOAD_HASHES:
        raise IngestError("HASH_UNAVAILABLE", f"unknown hash {hash_algorithm!r}; offload accepts {', '.join(OFFLOAD_HASHES)}")
    require([hash_algorithm, MHL_FORMAT_FOR[hash_algorithm]])
    dests = [Path(d).resolve() for d in destinations]
    if not dests:
        raise IngestError("NO_DESTINATION", "at least one --dest is required")
    if len(set(dests)) != len(dests):
        raise IngestError("DUPLICATE_DESTINATION", "the same destination was given twice; a second copy must be on a different path (ideally a different drive)")
    for d in dests:
        if _inside(d, src) or _inside(src, d):
            raise IngestError("DESTINATION_OVERLAPS_SOURCE", f"{d} and the source {src} contain one another")
    files, skipped = scan(src)
    reel_info, reel_warnings = card_reel(src, files, reel)
    name = reel_info["id"] if name_by == "reel" else src.name
    roots = [d / project / name if project else d / name for d in dests]
    started = datetime.now(timezone.utc)
    report = OffloadReport(source=str(src), destinations=[DestResult(root=str(r), warnings=list(reel_warnings)) for r in roots], skipped=skipped, hash=hash_algorithm,
                           dry_run=dry_run, started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"), card={"reel": reel_info, "name_by": name_by})
    mhl_format = MHL_FORMAT_FOR[hash_algorithm]
    histories: List[mhl.History] = []
    # Per destination: path -> (status, detail). A file failed on one destination is still listed by the others (ADR-004).
    per_dest: List[Dict[str, Tuple[str, str]]] = [{} for _ in roots]
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
        src_digests, outcome = _offload_file(f, st.st_size, [root / rel for root in roots], algorithms)
        statuses = []
        for dres, seen, (status, detail) in zip(report.destinations, per_dest, outcome):
            seen[rec.path] = (status, detail)
            statuses.append(status)
            if status == "copied":
                dres.copied += 1
            elif status == "skipped_identical":
                dres.skipped_identical += 1
            elif status == "conflict":
                dres.conflicts += 1
            else:
                dres.failed += 1
            if detail:
                rec.detail = detail
        rec.hashes = src_digests or {}
        rec.sha256 = rec.hashes.get("sha256")
        rec.status = "failed" if "failed" in statuses else "conflict" if "conflict" in statuses else "copied" if "copied" in statuses else "skipped_identical"
        report.files.append(rec)
    if not dry_run:
        for dres, root, hist, seen in zip(report.destinations, roots, histories, per_dest):
            dres.manifest = str(write_manifest(root, report, seen))
            _write_mhl(dres, hist, report, started, seen)
    report.ok = all(d.failed == 0 and d.conflicts == 0 and d.mhl_failed == 0 and not d.problems for d in report.destinations)
    report.finished_at = _now()
    return report


def _dest_status(f: FileRecord, statuses: Optional[Dict[str, Tuple[str, str]]]) -> str:
    """The status of f on one destination; without per-destination statuses, the file's overall status."""
    return statuses[f.path][0] if statuses is not None and f.path in statuses else f.status


def _write_mhl(dres: DestResult, hist: "mhl.History", report: OffloadReport, started: datetime,
               statuses: Optional[Dict[str, Tuple[str, str]]] = None) -> None:
    """Append this run's ASC MHL generation to one destination's history (process "transfer"): the files this
    destination has, copied or found identical, and verified. A file that failed here is not in it (ADR-004)."""
    if hist.problems:
        dres.warnings.append("no ASC MHL generation written: the destination's ASC MHL history has problems")
        return
    mhl_format = MHL_FORMAT_FOR[report.hash]
    items = []
    for f in report.files:
        if _dest_status(f, statuses) not in ("copied", "skipped_identical") or report.hash not in f.hashes:
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


def write_manifest(root: Path, report: OffloadReport, statuses: Optional[Dict[str, Tuple[str, str]]] = None) -> Path:
    """ingest-manifest.json of one destination: every file it has, verified. With this destination's statuses, the
    files whose copy failed here are listed under the optional "failed" key, so `verify` reports the copy incomplete."""
    algo = report.hash
    entries = [{"path": f.path, "size": f.size, algo: f.hashes[algo], "mtime": f.mtime} for f in report.files
               if algo in f.hashes and _dest_status(f, statuses) in ("copied", "skipped_identical")]
    doc = {"schema": MANIFEST_SCHEMA, "tool": f"ingest-skill {__version__}", "source": report.source, "created_at": _now(), "hash": algo, "files": entries}
    failed = [{"path": f.path, "size": f.size, "detail": statuses[f.path][1]} for f in report.files
              if statuses is not None and statuses.get(f.path, ("", ""))[0] == "failed"]
    if failed:  # optional, like "card": readers of ingest/manifest@1 that do not know it ignore it
        doc["failed"] = failed
    if report.card.get("reel"):  # optional: the reel this copy was made under, so inventory keeps an operator-given one
        doc["card"] = {"reel": report.card["reel"]["id"], "reel_source": report.card["reel"]["source"]}
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
    if not isinstance(doc.get("failed", []), list) or any(not isinstance(e, dict) or "path" not in e for e in doc.get("failed", [])):
        raise IngestError("MANIFEST_INVALID", f"{path}: \"failed\" must be a list of {{\"path\", ...}} entries")
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
                 "missing": [], "changed": [], "unlisted": [], "copy_failed": [], "failures": [], "warnings": warnings,
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
    # Files the offload that wrote the manifest failed to copy here (another destination may have them): incomplete.
    for e in (doc or {}).get("failed", []):
        if e["path"] not in listed:
            out["copy_failed"].append(e["path"])
            out["failures"].append({"code": "INCOMPLETE", "path": e["path"], "against": MANIFEST_NAME,
                                    "detail": f"the offload failed to copy this file here: {e.get('detail') or 'no detail recorded'}"})
    if hist is not None:
        out["failures"].extend(hist.problems)
    out["ok"] = not out["missing"] and not out["changed"] and not out["copy_failed"] and not (hist and hist.problems)
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


def _member(root: Path, rel: str, hashes: Dict[str, str], algo: Optional[str]) -> Dict:
    f = root / rel
    return {"path": rel, "size": f.stat().st_size, "hash": {"algorithm": algo, "value": hashes[rel]} if rel in hashes else None, **clip_facts(f)}


def inventory(target: str, reel: Optional[str] = None) -> Dict:
    """The clip list of a card or an offloaded folder, one entry per recording (cards.py): a recording split across
    files (spanned) is one clip whose `members` are listed in recording order. Each clip carries its sidecars (and
    what they reliably say), proxies and related files, its manifest hash(es) and measured facts. Reads only."""
    root = Path(target).resolve()
    if not root.is_dir():
        raise IngestError("SOURCE_NOT_FOUND", f"{target} is not a directory")
    check_reel(reel)
    hashes: Dict[str, str] = {}
    algo: Optional[str] = None
    recorded: Optional[Dict] = None
    if (root / MANIFEST_NAME).is_file():
        _, doc = load_manifest(str(root))
        algo = doc["hash"]
        hashes = {e["path"]: e[algo] for e in doc["files"]}
        recorded = doc.get("card") if isinstance(doc.get("card"), dict) else None
    files, _ = scan(root)
    rels = [f.relative_to(root).as_posix() for f in files]
    analysis = cards.analyse(root, rels)
    reel_info, reel_warnings = cards.derive_reel(analysis.clips, [(r, f.stat().st_size) for r, f in zip(rels, files)], operator=reel, recorded=recorded)
    clips = []
    total = 0.0
    for c in analysis.clips:
        members = [_member(root, rel, hashes, algo) for rel in c.members]
        first = members[0]
        facts = {k: v for k, v in first.items() if k not in ("path", "size", "hash")}
        if len(members) > 1:  # a spanned recording: its duration is the members' sum, known only when every member was probed
            durations = [m.get("duration_s") for m in members]
            facts["probed"] = all(m["probed"] for m in members)
            facts["duration_s"] = round(sum(durations), 3) if facts["probed"] and None not in durations else None
        total += facts.get("duration_s") or 0.0
        single = len(members) == 1
        clips.append({"clip_id": c.clip_id, "structure": c.structure, "path": first["path"], "spanned": not single, "incomplete": c.incomplete,
                      "size": sum(m["size"] for m in members),
                      "sha256": first["hash"]["value"] if single and algo == "sha256" and first["hash"] else None,
                      "hash": first["hash"] if single else None,
                      "members": [{k: m[k] for k in ("path", "size", "hash", "probed", "duration_s") if k in m} for m in members],
                      "sidecars": c.sidecars, "proxies": c.proxies, "related": c.related, "metadata": c.metadata, **facts})
    return {"schema": "ingest/inventory@2", "root": str(root),
            "card": {"reel": reel_info, "structures": analysis.structures, "index_files": analysis.index_files},
            "clips": clips, "clip_count": len(clips), "file_count": sum(len(c["members"]) for c in clips),
            "spanned_count": sum(1 for c in clips if c["spanned"]), "total_duration_s": round(total, 3),
            "warnings": reel_warnings + analysis.warnings,
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
