"""Progress, speed and ETA of an offload (ECOSYSTEM_ROADMAP.md step 10, ADR-009).

Each destination's writer thread adds the bytes it wrote and the time it spent writing and syncing them (its busy
time) to that destination's DestStats. bytes / busy time is the drive's own write speed, which is what tells a slow
drive apart: the copy as a whole runs at the pace of the slowest destination (ADR-004), so wall-clock rates would be
the same for every destination.

`offload --progress` writes one JSON object per line to stderr (stdout stays the one report document):
  {"event": "start", ...}, {"event": "progress", ...} at most every interval, {"event": "file", ...} when a file is
  done, {"event": "slow_destination", ...} once per destination, {"event": "done", ...}.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, TextIO

MB = 1_000_000
INTERVAL_S = 0.5           # at most one progress line per interval (file lines are always written)
SLOW_RATIO = 0.5           # a destination writing at less than half the destinations' median speed is slow
SLOW_MIN_BYTES = 64 * MB   # judged only once every destination compared has written this much


@dataclass
class DestStats:
    bytes: int = 0
    busy_s: float = 0.0

    def add(self, n: int, seconds: float) -> None:
        self.bytes += n
        self.busy_s += seconds

    def mb_s(self) -> Optional[float]:
        return round(self.bytes / self.busy_s / MB, 1) if self.busy_s > 0 and self.bytes else None


def json_lines(stream: Optional[TextIO] = None) -> Callable[[Dict], None]:
    """An emitter writing each event as one JSON line, flushed (stderr by default)."""
    lock = threading.Lock()

    def emit(event: Dict) -> None:
        out = stream or sys.stderr
        with lock:
            out.write(json.dumps(event, ensure_ascii=False) + "\n")
            out.flush()
    return emit


class Progress:
    """Counts the source bytes read (monotonic), and judges the destinations' write speeds while the copy runs."""

    def __init__(self, roots: Sequence[str], bytes_total: int, files_total: int, emit: Optional[Callable[[Dict], None]] = None,
                 interval_s: float = INTERVAL_S, slow_ratio: float = SLOW_RATIO, slow_min_bytes: int = SLOW_MIN_BYTES,
                 clock: Callable[[], float] = time.monotonic, eta: bool = True):
        self.roots = list(roots)
        self.dests: List[DestStats] = [DestStats() for _ in roots]
        self.bytes_total, self.files_total = bytes_total, files_total
        self.emit, self.interval_s, self.slow_ratio, self.slow_min_bytes, self.clock, self.eta = emit, interval_s, slow_ratio, slow_min_bytes, clock, eta
        self.done_bytes = 0
        self.files_done = 0
        self.file: Optional[str] = None
        self.file_size = 0
        self.file_bytes = 0
        self.t0 = clock()
        self.last_emit: Optional[float] = None
        self.slow: Dict[int, str] = {}     # destination index -> warning, once each
        self._lock = threading.Lock()

    def _send(self, event: Dict) -> None:
        if self.emit is not None:
            self.emit(event)

    def _speeds(self) -> List[Dict]:
        return [{"root": r, "bytes_written": d.bytes, "mb_s": d.mb_s()} for r, d in zip(self.roots, self.dests)]

    def _base(self, event: str) -> Dict:
        elapsed = self.clock() - self.t0
        rate = self.done_bytes / elapsed if elapsed > 0 else 0.0
        left = max(0, self.bytes_total - self.done_bytes)
        return {"event": event, "elapsed_s": round(elapsed, 3), "bytes_done": self.done_bytes, "bytes_total": self.bytes_total,
                "files_done": self.files_done, "files_total": self.files_total, "mb_s": round(rate / MB, 1) if rate else None,
                "eta_s": (round(left / rate, 1) if rate else None) if self.eta else None}

    def start(self) -> None:
        self._send(self._base("start"))

    def file_start(self, rel: str, size: int) -> None:
        self.file, self.file_size, self.file_bytes = rel, size, 0

    def read(self, n: int) -> None:
        """n more bytes of the current file read from the source (called by the reading thread, per chunk)."""
        with self._lock:
            self.done_bytes += n
            self.file_bytes += n
            now = self.clock()
            if self.last_emit is None or now - self.last_emit >= self.interval_s:
                self.last_emit = now
                self._send({**self._base("progress"), "file": self.file, "file_bytes": self.file_bytes, "file_size": self.file_size,
                            "destinations": self._speeds()})
            self._judge()

    def file_done(self, rel: str, size: int, status: str, read_bytes: int) -> None:
        """A file is finished; bytes it was expected to have but that were not read (a failed read, a skipped file)
        still count as done, so bytes_done only grows and ends at bytes_total."""
        with self._lock:
            self.done_bytes += max(0, size - read_bytes)
            self.files_done += 1
            self._send({**self._base("file"), "file": rel, "file_size": size, "status": status, "destinations": self._speeds()})
            self._judge()

    def _judge(self) -> None:
        """Warn once about a destination writing at less than slow_ratio x the median write speed of all destinations
        (each judged once it has written slow_min_bytes). The median, not the fastest: two fast drives that differ by
        chance are not told apart, and with two destinations a warning means under a third of the other's speed."""
        rates = {i: d.mb_s() for i, d in enumerate(self.dests) if d.bytes >= self.slow_min_bytes and d.mb_s() is not None}
        if len(rates) < 2:
            return
        ordered = sorted(rates.values())
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        for i, rate in rates.items():
            if i not in self.slow and rate < self.slow_ratio * median:
                msg = (f"{self.roots[i]} is writing at {rate} MB/s, under {int(self.slow_ratio * 100)}% of the destinations' median "
                       f"({round(median, 1)} MB/s): it sets the pace of the whole offload (a slow drive, port, hub or cable)")
                self.slow[i] = msg
                self._send({**self._base("slow_destination"), "root": self.roots[i], "dest_mb_s": rate, "median_mb_s": round(median, 1),
                            "detail": msg})

    def finish(self, ok: bool) -> None:
        self._send({**self._base("done"), "ok": ok, "destinations": self._speeds()})
