"""Hash algorithms: every digest a file needs is computed in one read pass.

- sha256 (default) and xxh64 are the two algorithms an offload can be run with (`--hash`).
- c4, md5, sha1, xxh64, xxh3 and xxh128 are the ASC MHL hash formats (ASC MHL v1.0 Appendix D). An offload records
  c4 in the ASC MHL when it runs with sha256 (ASC MHL has no sha256), xxh64 when it runs with xxh64.
- xxh64 / xxh3 / xxh128 need the optional `xxhash` package (`pip install 'ingest-skill[xxhash]'`). Without it they
  are UNAVAILABLE: a run that needs one is refused before anything is written; sha256, c4, md5 and sha1 are stdlib.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Dict, Iterable, List

from .errors import IngestError

try:  # optional extra: pip install 'ingest-skill[xxhash]'
    import xxhash  # type: ignore
except ImportError:  # pragma: no cover - depends on the environment
    xxhash = None

CHUNK = 8 * 1024 * 1024
OFFLOAD_HASHES = ("sha256", "xxh64")
DEFAULT_HASH = "sha256"
# ASC MHL hash formats, in the element order of the ASC MHL XML Schema (HashType).
MHL_FORMATS = ("c4", "md5", "sha1", "xxh128", "xxh3", "xxh64")
# The ASC MHL format recorded for each offload hash.
MHL_FORMAT_FOR = {"sha256": "c4", "xxh64": "xxh64"}
XXHASH_ALGORITHMS = ("xxh64", "xxh3", "xxh128")
INSTALL_HINT = "pip install 'ingest-skill[xxhash]'"
C4_CHARSET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def available(algorithm: str) -> bool:
    if algorithm in XXHASH_ALGORITHMS:
        return xxhash is not None
    return algorithm in ("sha256",) + MHL_FORMATS


def require(algorithms: Iterable[str]) -> None:
    """Refuse (HASH_UNAVAILABLE) when an algorithm cannot be computed here."""
    for a in sorted(set(algorithms)):
        if a not in ("sha256",) + MHL_FORMATS:
            raise IngestError("HASH_UNAVAILABLE", f"unknown hash algorithm {a!r}; offload accepts {', '.join(OFFLOAD_HASHES)}")
        if not available(a):
            raise IngestError("HASH_UNAVAILABLE", f"{a} needs the optional xxhash package, which is not installed: {INSTALL_HINT}")


def _factory(algorithm: str) -> Callable:
    if algorithm == "sha256":
        return hashlib.sha256
    if algorithm == "c4":
        return hashlib.sha512
    if algorithm in ("md5", "sha1"):
        # Integrity checksums, not security: allowed on FIPS-restricted builds too.
        return lambda: hashlib.new(algorithm, usedforsecurity=False)
    require([algorithm])
    return {"xxh64": xxhash.xxh64, "xxh3": xxhash.xxh3_64, "xxh128": xxhash.xxh3_128}[algorithm]


def c4_encode(sha512_digest: bytes) -> str:
    """SMPTE ST 2114 C4 ID: the SHA-512 digest in base58, left-padded to 88 characters with '1', prefixed 'c4'."""
    n = int.from_bytes(sha512_digest, "big")
    out: List[str] = []
    while n:
        n, r = divmod(n, 58)
        out.append(C4_CHARSET[r])
    return "c4" + "".join(reversed(out)).rjust(88, "1")


class MultiHash:
    """Several digests of one byte stream. Hex digests are lowercase; xxHash digests are the canonical (big-endian)
    form, which is the ASC MHL encoding; c4 is a C4 ID."""

    def __init__(self, algorithms: Iterable[str]):
        self._h = {a: _factory(a)() for a in sorted(set(algorithms))}

    def update(self, data: bytes) -> None:
        for h in self._h.values():
            h.update(data)

    def digests(self) -> Dict[str, str]:
        return {a: c4_encode(h.digest()) if a == "c4" else h.hexdigest() for a, h in self._h.items()}


def hash_file(path: Path, algorithms: Iterable[str]) -> Dict[str, str]:
    m = MultiHash(algorithms)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            m.update(chunk)
    return m.digests()


def hash_bytes(data: bytes, algorithm: str) -> str:
    m = MultiHash([algorithm])
    m.update(data)
    return m.digests()[algorithm]
