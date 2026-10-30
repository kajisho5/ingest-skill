"""Benchmark note for ADR-004 (not a test, not a CI gate): offload one synthetic card to N destinations and report
the wall time and how many bytes were read from the source.

    python benchmarks/fanout.py --size-mb 512 --dests 3 [--hash xxh64] [--reader-mbps 160] [--src PATH_TO_SRC]

--reader-mbps throttles reads of the source (only) to model a card reader that is slower than the drives; without it
the source is read at whatever the local disk and page cache give, which says little about a card on location.
--src runs another checkout's src/ (e.g. the version before ADR-004) for a before / after comparison.
"""
from __future__ import annotations

import argparse
import builtins
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mb", type=int, default=256)
    ap.add_argument("--files", type=int, default=4)
    ap.add_argument("--dests", type=int, default=3)
    ap.add_argument("--hash", default="sha256")
    ap.add_argument("--reader-mbps", type=float, default=0.0)
    ap.add_argument("--src", default=str(Path(__file__).resolve().parents[1] / "src"))
    a = ap.parse_args()
    sys.path.insert(0, a.src)
    from ingest_skill.core import offload

    tmp = Path(tempfile.mkdtemp(prefix="ingest-bench-")).resolve()
    card = tmp / "CARD_A" / "CLIP"
    card.mkdir(parents=True)
    per = a.size_mb * 1024 * 1024 // a.files
    for i in range(a.files):
        (card / f"C{i + 1:04d}.MP4").write_bytes(os.urandom(per))
    real_open, read = builtins.open, [0]

    class Throttled:
        def __init__(self, f):
            self._f = f

        def read(self, *x):
            data = self._f.read(*x)
            read[0] += len(data)
            if a.reader_mbps:
                time.sleep(len(data) / (a.reader_mbps * 1e6))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *e):
            self._f.close()

        def __getattr__(self, n):
            return getattr(self._f, n)

    def opener(file, mode="r", *x, **k):
        f = real_open(file, mode, *x, **k)
        return Throttled(f) if not isinstance(file, int) and "r" in mode and "b" in mode and card in Path(os.fspath(file)).resolve().parents else f

    builtins.open = opener
    try:
        t = time.perf_counter()
        r = offload(str(card.parent), [str(tmp / f"ssd{i + 1}") for i in range(a.dests)], hash_algorithm=a.hash)
        dt = time.perf_counter() - t
    finally:
        builtins.open = real_open
        shutil.rmtree(tmp, ignore_errors=True)
    total = a.files * per
    print(f"ok={r.ok} hash={a.hash} dests={a.dests} size={total / 1e6:.0f} MB reader={a.reader_mbps or 'unthrottled'} MB/s "
          f"time={dt:.2f} s source_read={read[0] / total:.2f}x ({read[0] / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
