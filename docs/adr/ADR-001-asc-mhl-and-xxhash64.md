# ADR-001: ASC MHL History per Destination, and xxHash64 as an Optional Hash

Status: Accepted

## Context

Before this change, `ingest offload` checked every copy with sha256 and wrote only its own `ingest-manifest.json`. A DIT hands footage to post houses and other tools, and they expect an **ASC MHL** (ASC Media Hash List) history next to the media. Many offload tools also default to an **xxHash** checksum, because it is much faster than sha256 on large cards. `ECOSYSTEM_ROADMAP.md` step 3 asks for an MHL per destination, and for `verify` to accept sha256 or xxh64.

In this workflow reliability matters more than speed. Three rules follow from that: a copy must never count as verified on weaker evidence than before, a missing optional component must refuse cleanly, and the output must be readable by the reference tool.

**Primary sources** (read on 2026-10-30):

- **ASC MHL Specification v1.0** (15 March 2022). `Specification/ASCMHL_Specification_v1.0.pdf` in [ascmitc/mhl-specification](https://github.com/ascmitc/mhl-specification), commit `54b7eea`. Section numbers below refer to this document.
- **ASC MHL XML schemas** `xsd/ASCMHL.xsd` and `xsd/ASCMHLDirectory.xsd` in the reference implementation [ascmitc/mhl](https://github.com/ascmitc/mhl), commit `0fb61f1` (2025-07-04).
  - The schema in the repository puts `hashes` before `metadata` in `HashListType`. The PDF's section 6.4.1 lists them the other way round.
  - We write no `metadata`, so both orders validate.
- **Reference tool `ascmhl` 1.2** (PyPI, the same repository). Used only for cross-checking (see Verification). It is not a dependency.

## Decision

1. **Every destination gets an ASC MHL history, in addition to `ingest-manifest.json`.** `ingest-manifest.json` is unchanged in shape. Its schema stays `ingest/manifest@1`. `"hash"` is `"sha256"` or `"xxh64"`, and each file entry carries the digest under that key.
   - Layout (§5.3.1, §6.3, §7.3): `<dest>/<project>/<card>/ascmhl/` holds:
     - one immutable manifest per run, named `NNNN_<card folder>_<YYYY-MM-DD>_<HHMMSS>Z.mhl`. The UTC time is taken at the start of the run.
     - `ascmhl_chain.xml`, which lists each manifest with its sequence number and its C4 ID.
   - Manifest (§6, Appendix A):
     - root element `hashlist version="2.0"` in namespace `urn:ASC:MHL:v2.0`, written as the default namespace, as in the specification's examples.
     - `creatorinfo`: `creationdate`, `hostname`, `tool version=…`.
     - `processinfo`: `process` = **`transfer`**. Per §6.4.6 this means the tool copied the files and guarantees that source and destination are identical. That is true here: every copy is read back and every digest is compared.
     - `ignore`: the patterns described in item 5.
     - one `hash` element per file, with a `path` element carrying `size` and `lastmodificationdate`, then the hash elements in the schema's order.
   - Chain file (§7, Appendix E): `ascmhldirectory` in namespace `urn:ASC:MHL:DIRECTORY:v2.0`. Each `hashlist` element has a `sequencenr` (1, 2, 3, …), a `path` and a `c4`.
   - Chain updates are written to a temporary file, synced to disk, then renamed into place. A manifest file is never replaced (§5.2: "immutable and shall not be altered").
2. **The MHL hash format follows `--hash`.**

   | `--hash` | Read-back and `ingest-manifest.json` | Recorded in the ASC MHL | Needs |
   |---|---|---|---|
   | `sha256` (default) | sha256 | **c4** | standard library only |
   | `xxh64` | xxh64 | xxh64 | the `xxhash` extra |

   - ASC MHL has no sha256 (Appendix D lists only md5, sha1, c4, xxh64, xxh3 and xxh128). The default therefore records **c4**.
   - c4 is SMPTE ST 2114: SHA-512 encoded in base58, padded to 90 characters. It is cryptographic, needs only the standard library, and is the format ASC MHL itself uses to reference manifests.
   - md5 and sha1 were rejected for the default. They are weaker, and choosing one would mean picking a broken algorithm on purpose.
   - The c4 digest is computed in the same read pass as sha256 and is compared again at read-back. Every copy is still checked at least as strongly as before.
   - xxh64 uses seed 0 and the canonical big-endian hex digest, as Appendix D requires.
3. **xxHash is an optional extra, not a dependency.** The repository was "Python 3.9+, standard library only" (README), with `dependencies = []`, so that stays true for the default path.
   - `pip install 'ingest-skill[xxhash]'` adds [`xxhash`](https://pypi.org/project/xxhash/) `>=3.0`. It provides xxh64, and also xxh3 / xxh128 for reading other tools' histories.
   - A pure-Python XXH64 was rejected. It is too slow for camera cards, and a hand-written hash is exactly the kind of code that should not guard footage.
   - Without the extra, xxh64 is **UNAVAILABLE**:
     - `offload --hash xxh64` and `verify` of an xxh64 manifest stop with `HASH_UNAVAILABLE` (exit 2) before anything is written or hashed. The message names the install command.
     - `ingest doctor` reports `xxhash: unavailable` and `hash_algorithms.xxh64: unavailable`. `ok` stays true, because the default path works.
     - The contract lists `xxhash` under `capabilities.optional`.
4. **Generations and actions** (§5.2, §5.6.5, §6.5.1).
   - Each offload run appends one generation containing every file it copied or found identical. A resumed run therefore records `verified` hashes for files that are already present.
   - The action for a file is:
     - `original` when the history has no earlier record of the file.
     - `verified` when an earlier record matches.
     - `failed` when an earlier record differs. The destination then reports `mhl_failed`, and the run is not ok.
   - The earlier formats of a file that can be computed here (c4, md5, sha1, and xxh* when the extra is installed) are added to the same read pass. They are checked, and recorded again with the same action.
   - Earlier records in a format that cannot be computed here are listed under `warnings` and are not checked.
   - No generation is written when a destination's history is damaged (`MHL_INVALID`) or when no file was copied or verified (§6.4.1 requires `hashes` or `references`).
5. **The source's own history continues on each copy.** This covers a shuttle drive copied from a backup, or a card that arrives with a history.
   - At the source's top level, `ascmhl/` and `ingest-manifest.json` belong to the folder, not to its media. They are listed in `skipped` (`skipped_ascmhl`, `skipped_manifest`) and are not copied as media files.
   - When a destination has no history yet, the source's chain and its listed manifests are copied with the same verified copy, and the new generation is appended after them.
   - If the destination already has a different history, nothing is merged and `MHL_INVALID` is reported.
   - A damaged source history (a C4 mismatch, a missing manifest, or `ascmhl/` without a chain file) is not carried. It is reported as `MHL_INVALID`, so the run is not ok. The media are still copied and verified.
   - The ignore patterns are the ASC MHL defaults (`.DS_Store`, `ascmhl`, `ascmhl/`) plus the card housekeeping ingest never copies and `/ingest-manifest.json`. That file is rewritten on every run, so it cannot carry a stable hash.
6. **`verify` accepts sha256 or xxh64, and reads the ASC MHL history.** It reads `ingest-manifest.json`, the history, or both, and re-hashes each file **once** for every algorithm either one records.
   - A sha256 manifest from before this change still verifies. A missing `"hash"` is read as sha256.
   - A history alone is enough. This covers a folder whose `ingest-manifest.json` was lost, or one written by another ASC MHL tool.
   - Each manifest is checked against its C4 ID in the chain before its records are trusted. A manifest with no chain entry is a warning.
   - Failures are structured: `failures: [{"code", "path", "hash", "expected", "actual", "against"}]` with `VERIFY_MISMATCH`, `FILE_MISSING` or `MHL_INVALID`. The existing `missing` / `changed` / `unlisted` lists and the exit codes are unchanged.
   - `verify` **writes nothing**. §5.6.4 asks a tool that verifies "using an ASC MHL History" to append a generation. `ingest verify` is a read-only measurement (its contract declares `writes_media: false`), so recording a verification is left to a future explicit option. `ascmhl create` does it today.
7. **Not implemented in this step** (all listed in `not_provided`):
   - directory hashes and `roothash` (§6.5.2–6.5.3, optional);
   - rename / `previousPath` (§5.6.6);
   - flatten (§5.6.7) and collections (§8);
   - nested histories inside a card (§5.3.2). A deeper `ascmhl/` is copied as files and ignored by the pattern `ascmhl`, which is what the reference tool does;
   - `author` / `location` metadata;
   - legacy MHL v1 files.
   - An MHL or chain that contains a DTD is refused, so a file found on a card cannot trigger entity expansion.

## Verification

- **Unit tests** (`tests/test_ingest.py`) cover, for both hashes:
  - an MHL written in every destination, whose hashes equal independently computed c4 / xxh64 values, whose chain C4 matches, and whose element order matches the schema;
  - `verify` passing on an intact copy, and failing with `VERIFY_MISMATCH` against both `ingest-manifest.json` and `ascmhl/` on a flipped byte;
  - verification from the history alone;
  - a resume that appends a `verified` generation;
  - a tampered manifest breaking the chain;
  - a carried-over source history, including a `failed` record for a source that rotted after it was recorded;
  - the HASH_UNAVAILABLE path, run without the extra.
- **Test vectors.** The c4 vectors come from the reference implementation (`ascmhl.hasher.C4`). The xxh64 vectors are the canonical seed-0 values.
- **CI** runs the suite twice: with the standard library only, then with `.[xxhash]`.
- **Cross-checks run once while writing this ADR** (not in CI):
  - every generated `.mhl` validated with `xmllint` against `xsd/ASCMHL.xsd`, and every chain file against `xsd/ASCMHLDirectory__combined.xsd`;
  - `ascmhl diff` reported no unknown or missing files;
  - `ascmhl create` (which verifies against the history) passed on intact copies for c4 and xxh64, and failed with "hash mismatch" on a flipped byte for both;
  - `ingest verify` read a history to which `ascmhl` had appended an xxh128 generation with directory hashes.

## Consequences

- With `--hash sha256`, every byte is hashed with SHA-256 and SHA-512 together. This is slower than sha256 alone. `--hash xxh64` is the fast path for large cards.
- `offload` now creates `ascmhl/` and appends a manifest on every non-dry run, including a resume. Re-running is still safe: identical files are skipped, and the history grows by one generation.
- New error codes: `HASH_UNAVAILABLE` (exit 2), and `FILE_MISSING` and `MHL_INVALID` (exit 3).
