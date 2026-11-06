"""Recordings still being written (growing files): `ingest offload --follow` (ECOSYSTEM_ROADMAP.md step 8, ADR-007).

A file whose size or modification time is still changing is copied as it grows:
- each pass reads only the bytes appended since the last verified offset, appends them to every destination (in place,
  so the partial copy can already be opened in an editor), fsyncs, and reads the appended range of every destination
  back; the offset advances only when that range matches what was read from the source;
- the copy is listed under the manifest's optional "growing" key (`"growing": true`, the verified size and the hash of
  those bytes), never under "files", and it is not in the ASC MHL;
- it is sealed (hashed, verified and written into the manifest's "files" and the ASC MHL) only once its size and
  modification time have been unchanged for the settle window. Sealing reads the whole destination back; when the
  source was read in more than one pass it is also re-hashed in full, so bytes the recorder rewrote before its end
  (a header updated at close) are found: the file is then copied again in full.

Whether a recorder still holds a file open cannot be told portably (no API on macOS / Windows / network shares), so
the settle window is the only end-of-recording signal.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import core
from .errors import IngestError
from .hashing import MultiHash, hash_file, hash_range

DEFAULT_SETTLE_S = 30.0
DEFAULT_POLL_S = 2.0
_END, _ABORT = object(), object()


class _Appender(threading.Thread):
    """One destination of a growing copy: keeps the first `keep` bytes of dst, appends the source bytes beyond them
    (the chunks start at source offset `pos0` <= keep), flushes and fsyncs. On any error, or when the source read does
    not complete, dst is cut back to `keep` bytes (removed when keep is 0 and this pass created it)."""

    def __init__(self, dst: Path, keep: int, pos0: int):
        super().__init__(name=f"ingest-append {dst}", daemon=True)
        self.dst, self.keep, self.pos0 = dst, keep, pos0
        self.chunks: "queue.Queue" = queue.Queue(maxsize=core.QUEUE_DEPTH)
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        fo = None
        created = False
        item = None
        pos = self.pos0
        try:
            try:
                self.dst.parent.mkdir(parents=True, exist_ok=True)
                created = not self.dst.exists()
                fo = open(self.dst, "wb" if created else "r+b")
                fo.seek(self.keep)
                fo.truncate(self.keep)
            except OSError as e:
                self.error = e
            while True:
                item = self.chunks.get()
                if item is _END or item is _ABORT:
                    break
                if self.error is None and pos + len(item) > self.keep:
                    try:
                        fo.write(item[max(0, self.keep - pos):])
                    except OSError as e:
                        self.error = e
                pos += len(item)
            if self.error is None and item is _ABORT:
                self.error = IngestError("INCOMPLETE", f"{self.dst}: not appended, the source read did not complete")
            if self.error is None:
                fo.flush()
                os.fsync(fo.fileno())
        except Exception as e:  # noqa: BLE001 - this destination's failure only
            self.error = self.error or e
        finally:
            if fo is not None:
                try:
                    if self.error is not None:
                        fo.truncate(self.keep)
                    fo.close()
                except OSError:
                    pass
            if self.error is not None and created and self.keep == 0:
                try:
                    self.dst.unlink()
                except OSError:
                    pass


def _give(w: _Appender, item: object) -> None:
    while True:
        try:
            w.chunks.put(item, timeout=1.0)
            return
        except queue.Full:
            if not w.is_alive():
                return


@dataclass
class Growing:
    """One source file copied as it grows, to every destination that takes part."""
    rel: str
    src: Path
    dsts: List[Path]
    algorithms: List[str]
    hash_algorithm: str
    keep: List[int]                     # per destination: bytes of dst known good (verified, or an earlier partial to check)
    prefix: List[Optional[Dict]]        # per destination: the manifest "growing" entry describing dst, or None
    outcome: List[Optional[Tuple[str, str]]]   # per destination: a final (status, detail) once it left the copy
    offset: int = 0                     # source bytes read, appended to every active destination and verified there
    run: MultiHash = None               # digests of source[0:offset)
    passes: int = 0
    single_read: bool = False           # source[0:offset) was read in one pass with its size and mtime unchanged
    last_stat: Optional[Tuple[int, int]] = None
    changed_at: float = 0.0             # clock() when size / mtime last changed
    reread: bool = False
    recopied: bool = False
    detail: str = ""

    def __post_init__(self):
        if self.run is None:
            self.run = MultiHash(self.algorithms)

    def active(self) -> List[int]:
        return [i for i, o in enumerate(self.outcome) if o is None]

    def leave(self, i: int, status: str, detail: str) -> None:
        self.outcome[i] = (status, detail)


def start(rel: str, src: Path, dsts: Sequence[Path], algorithms: Sequence[str], hash_algorithm: str,
          partials: Sequence[Dict[str, Dict]], src_size: int) -> Optional[Growing]:
    """A growing copy of src, or None when a destination already has a complete copy of the current size (the file is
    then offloaded the ordinary way, which finds it identical or a conflict). A destination file that is neither absent
    nor an ingest-owned partial (listed under its manifest's "growing") is a conflict there: never overwritten."""
    g = Growing(rel, src, list(dsts), list(algorithms), hash_algorithm, keep=[0] * len(dsts), prefix=[None] * len(dsts), outcome=[None] * len(dsts))
    for i, d in enumerate(dsts):
        entry = partials[i].get(rel)
        try:
            size = d.stat().st_size if d.exists() else None
        except OSError as e:
            g.leave(i, "failed", f"{d}: {e}")
            continue
        if size is None:
            continue
        if entry is not None and size >= entry["size"]:
            g.keep[i], g.prefix[i] = entry["size"], entry      # ours: bytes beyond entry["size"] were never verified
        elif size == src_size:
            return None
        else:
            g.leave(i, "conflict", f"{d} exists with different content; not overwritten")
    return g


def grow(g: Growing) -> bool:
    """One pass: read source[offset:size] once, append it to every active destination, read the appended range back.
    Returns False when the source shrank or could not be read (every destination keeps its last verified bytes)."""
    try:
        before = os.stat(g.src)
    except OSError as e:
        g.detail = str(e)
        return False
    end = before.st_size
    if end < g.offset:
        g.detail = str(IngestError("SOURCE_CHANGED", f"{g.src} is shorter than the {g.offset} bytes already copied"))
        return False
    act = g.active()
    if end == g.offset and g.passes:
        return True
    writers = {i: _Appender(g.dsts[i], g.keep[i], g.offset) for i in act if g.keep[i] <= end}
    for i in act:
        if i not in writers:
            g.leave(i, "conflict", f"{g.dsts[i]} is longer than the source; not overwritten")
    for w in writers.values():
        w.start()
    run, rng = g.run.copy(), MultiHash([g.hash_algorithm])
    ok, finish = False, _ABORT
    try:
        with open(g.src, "rb") as fi:
            fi.seek(g.offset)
            left = end - g.offset
            while left > 0:
                chunk = fi.read(min(core.CHUNK, left))
                if not chunk:
                    raise IngestError("SOURCE_CHANGED", f"{g.src} became shorter while it was read")
                for w in writers.values():
                    if w.error is None:
                        _give(w, chunk)
                run.update(chunk)
                rng.update(chunk)
                left -= len(chunk)
        after = os.stat(g.src)
        ok, finish = True, _END
    except Exception as e:  # noqa: BLE001 - the source failed: nothing advances, every destination is cut back
        g.detail = str(e)
    finally:
        for w in writers.values():
            _give(w, finish)
        for w in writers.values():
            w.join()
    if not ok:
        return False
    want = rng.digests()[g.hash_algorithm]
    start_at = g.offset
    checks = [i for i, w in writers.items() if w.error is None]
    for i, w in writers.items():
        if w.error is not None:
            g.leave(i, "failed", str(w.error) if str(w.error).startswith(str(g.dsts[i])) else f"{g.dsts[i]}: {w.error}")

    def back(i: int):
        try:
            if g.dsts[i].stat().st_size != end:
                return OSError(f"{g.dsts[i]}: {g.dsts[i].stat().st_size} bytes after the append, expected {end}")
            return hash_range(g.dsts[i], start_at, end, [g.hash_algorithm])[g.hash_algorithm]
        except Exception as e:  # noqa: BLE001
            return e

    with ThreadPoolExecutor(max_workers=max(1, len(checks)), thread_name_prefix="ingest-readback") as pool:
        results = list(pool.map(back, checks))
    for i, got in zip(checks, results):
        if isinstance(got, BaseException) or got != want:
            resumed = g.keep[i] > start_at
            _cut(g.dsts[i], g.keep[i])
            if resumed:
                g.leave(i, "conflict", f"{g.dsts[i]}: its earlier partial copy does not match the source; left at {g.keep[i]} bytes, not overwritten")
            else:
                g.leave(i, "failed", str(got) if isinstance(got, BaseException) else
                        f"{g.dsts[i]}: VERIFY_MISMATCH read-back of bytes {start_at}-{end} {g.hash_algorithm} {got} != {want} (source)")
        else:
            g.keep[i] = end
            g.prefix[i] = None
    g.single_read = g.passes == 0 and start_at == 0 and (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    g.run, g.offset = run, end
    g.passes += 1
    return True


def _cut(path: Path, size: int) -> None:
    try:
        if size == 0:
            path.unlink()
        else:
            with open(path, "r+b") as f:
                f.truncate(size)
    except OSError:
        pass


def seal(g: Growing) -> Optional[Dict[str, str]]:
    """Hash and verify a copy whose source has stopped changing. Returns the source digests, or None when the source
    changed again meanwhile (the copy goes on growing). When the bytes read in several passes are not the source's
    bytes any more (rewritten before its end), the partial copies are removed and the file is copied again in full."""
    st = os.stat(g.src)
    if st.st_size != g.offset:
        return None
    digests = g.run.digests()
    if not g.single_read:
        g.reread = True
        again = hash_file(g.src, g.algorithms)
        st2 = os.stat(g.src)
        if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            return None
        if again != digests:
            act = g.active()
            for i in act:
                _cut(g.dsts[i], 0)
            g.recopied = True
            g.detail = "rewritten before its end while it was recorded (e.g. a header updated at close): copied again in full"
            src_digests, outcome = core._offload_file(g.src, st.st_size, [g.dsts[i] for i in act], g.algorithms)
            for i, o in zip(act, outcome):
                g.leave(i, *o)
            return src_digests
    act = g.active()
    for i, back in zip(act, core._hash_each([g.dsts[i] for i in act], g.algorithms)):
        d = g.dsts[i]
        if isinstance(back, BaseException):
            g.leave(i, "failed", str(back))
        elif back == digests:
            try:
                os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                pass
            g.leave(i, "copied", "")
        else:
            _cut(d, 0)
            g.leave(i, "failed", str(core._mismatch(d, back, digests)))
    return digests


def entry(g: Growing, i: int, mtime: Optional[str]) -> Optional[Dict]:
    """The manifest "growing" entry of destination i: what it holds of this file, verified, or None."""
    if g.prefix[i] is not None:
        return g.prefix[i]
    if g.keep[i] == 0 or g.keep[i] != g.offset:
        return None
    return {"path": g.rel, "size": g.offset, g.hash_algorithm: g.run.digests()[g.hash_algorithm], "mtime": mtime, "growing": True}


@dataclass
class FollowState:
    """Per source file, while following: "observe" (an old file seen once), "growing", or done."""
    phase: Dict[str, str] = field(default_factory=dict)
    seen: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    growing: Dict[str, Growing] = field(default_factory=dict)


def follow(ctx: "core._Offload", settle_s: float, poll_s: float, follow_for_s: Optional[float],
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Dict:
    """Poll the source until every file in it is sealed (or failed), `follow_for_s` has passed, or Ctrl-C."""
    stt = FollowState()
    t0 = clock()
    polls = 0
    stopped = "all_sealed"
    try:
        while True:
            files, skipped = core.scan(ctx.src)
            ctx.add_skipped(skipped)
            now, wall = clock(), time.time()
            present = {f.relative_to(ctx.src).as_posix() for f in files}
            for rel in [r for r, p in stt.phase.items() if p != "done" and r not in present]:
                stt.phase[rel] = "done"   # gone from the source (deleted or renamed by the recorder)
                g = stt.growing.get(rel)
                if g is not None:
                    g.detail = f"{g.src} disappeared from the source while it was followed"
                    ctx.finish_growing(g, None, None, source_failed=True)
            for f in files:
                rel = f.relative_to(ctx.src).as_posix()
                if stt.phase.get(rel) == "done":
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                key = (st.st_size, st.st_mtime_ns)
                phase = stt.phase.get(rel)
                if phase is None:
                    if any(rel in p for p in ctx.partials) or wall - st.st_mtime < settle_s:
                        phase = "growing"
                    else:
                        stt.phase[rel], stt.seen[rel] = "observe", key
                        continue
                if phase == "observe":
                    if key == stt.seen[rel]:
                        status = ctx.copy_file(f, rel, st)
                        if status != "source_changed":
                            stt.phase[rel] = "done"
                            continue
                    phase = "growing"
                if rel not in stt.growing:
                    g = start(rel, f, ctx.dest_paths(rel), ctx.algorithms(rel), ctx.hash, ctx.partials, st.st_size)
                    if g is None:
                        stt.phase[rel], stt.seen[rel] = "observe", key
                        continue
                    g.changed_at = now - max(0.0, wall - st.st_mtime)
                    g.last_stat = key
                    stt.growing[rel] = g
                stt.phase[rel] = "growing"
                g = stt.growing[rel]
                if key != g.last_stat:
                    g.last_stat, g.changed_at = key, now
                if not g.active():
                    ctx.finish_growing(g, None, st)
                    stt.phase[rel] = "done"
                    continue
                if not grow(g):
                    ctx.finish_growing(g, None, st, source_failed=True)
                    stt.phase[rel] = "done"
                    continue
                if not g.active():
                    ctx.finish_growing(g, None, st)
                    stt.phase[rel] = "done"
                    continue
                if now - g.changed_at >= settle_s:
                    digests = seal(g)
                    if digests is not None:
                        ctx.finish_growing(g, digests, st)
                        stt.phase[rel] = "done"
                        continue
                    g.changed_at = now
            ctx.write_manifests(stt.growing.values(), {r for r, p in stt.phase.items() if p != "done"})
            polls += 1
            if all(p == "done" for p in stt.phase.values()):
                break
            if follow_for_s is not None and clock() - t0 >= follow_for_s:
                stopped = "follow_for"
                break
            sleep(poll_s)
    except KeyboardInterrupt:
        stopped = "interrupted"
    unsealed = [g for r, g in stt.growing.items() if stt.phase.get(r) != "done"]
    for g in unsealed:
        ctx.leave_growing(g)
    return {"settle_s": settle_s, "poll_s": poll_s, "follow_for_s": follow_for_s, "polls": polls, "stopped": stopped,
            "files": {r: {"passes": g.passes, "sealed": stt.phase.get(r) == "done" and g.outcome.count(("copied", "")) > 0,
                          "reread_at_seal": g.reread, "recopied": g.recopied, "bytes_verified": g.offset}
                      for r, g in sorted(stt.growing.items())}}
