---
name: ingest-skill
description: Offload camera cards and recorders safely - copy a card folder to one or more drives with a sha256 (or xxh64) read-back of every file, write an ASC MHL history per drive, never overwrite, resume an interrupted copy, re-verify a backup later and list the clips (duration, codecs, resolution, timecode). Use when asked to ingest, offload, back up or verify footage, make an MHL, or make a clip list of a shoot (one entry per recording, spanned files grouped, camera sidecars, reel / card id). Local only, no network.
---

# ingest-skill

Run `ingest <command> ... --json` (installed by `pip install -e .`). Every command prints one JSON document.

| Task | Command |
|---|---|
| Copy a card to two drives | `ingest offload /Volumes/CARD_A --dest /Volumes/SSD1/Footage --dest /Volumes/SSD2/Footage --project 2026-10-30_event --json` |
| See what would be copied | add `--dry-run` (writes nothing) |
| Use xxHash64 instead of sha256 | add `--hash xxh64` (needs `pip install -e ".[xxhash]"`; check with `ingest doctor --json`) |
| Re-check a backup | `ingest verify /Volumes/SSD1/Footage/2026-10-30_event/CARD_A --json` |
| Clip list | `ingest inventory /Volumes/SSD1/Footage/2026-10-30_event/CARD_A --json` |
| Name the copy after the card's reel | add `--name-by reel` to offload (`--reel B007` to set it yourself) |
| What this skill declares | `ingest contract --json`, `ingest doctor --json` |

## What it guarantees

- The source card is only read. It is never modified, deleted or formatted.
- Each copy is written to a temporary name, flushed to disk, renamed, then read back; it counts as copied only when the read-back sha256 equals the hash taken while reading the source.
- The card is read once per file, however many destinations: the same bytes are hashed and written to every drive in parallel, then each drive is read back on its own. If one drive fails (full, unplugged), only that drive's copy of the file fails; the other drives are still complete and verified.
- An existing file at the destination is never overwritten: identical content is skipped (so a re-run resumes an interrupted offload), different content is a **conflict** and the run is incomplete (exit 3).
- Each destination gets `ingest-manifest.json` (path, size, sha256 or xxh64, mtime of every file) and an ASC MHL history in `ascmhl/`: one new `.mhl` generation per run (process `transfer`; c4 with sha256, xxh64 with xxh64) and `ascmhl_chain.xml`. A source folder's own `ascmhl/` history is carried into each copy and continued.
- `verify` re-hashes against the manifest and the ASC MHL history (sha256, xxh64, c4, md5, sha1; xxh3 / xxh128 with the extra) and reports missing, changed and unlisted files; each entry of `failures` has a `code`, the `hash`, the `expected` and the `actual` value. It writes nothing.
- Every file of the card is copied unchanged, whatever its structure: sidecars, index files, playlists and proxies are needed by NLEs, so never copy only the media files.
- Card housekeeping (`.DS_Store`, `._*`, `.Spotlight-V100`, `System Volume Information`, ...) and symbolic links are listed as skipped, never copied or followed.
- A destination inside the source (or the other way round), the same destination twice, or no destination is refused before anything is written (exit 2).

Exit codes: `0` ok, `2` refused (including `HASH_UNAVAILABLE`: xxh64 without the xxhash extra, and `REEL_INVALID`), `3` incomplete (conflict, failure, missing or changed file, damaged ASC MHL history).

## Report it honestly

- Say "verified" only when `ok` is true. If `ok` is false, name the conflicts / failures from the JSON; do not call the backup done.
- When one destination has `failed` files, name that drive: the other destinations are complete. Its `ingest-manifest.json` lists the files under `failed` and its `verify` is not ok (`copy_failed`); re-run the same offload after fixing the drive to complete it (only the missing files are written).
- `mhl_failed` or an `action="failed"` record means a file differs from what its ASC MHL history recorded earlier: report it as damage, not as a new version.
- If xxh64 is `unavailable` in `doctor`, say so and offer sha256 (the default) or installing the extra; do not fall back silently.
- `inventory` lists recordings, not files: a `spanned` clip's `members` are one recording in order; an `incomplete` one continues on another card. Report the clip count from `clip_count`, and the reel with its `source` (`operator`, `manifest`, `clip_names`, `fingerprint`); a `fingerprint` reel is not a camera reel.
- Sidecar `metadata` holds only what the camera wrote; a missing key is unknown. `warnings` (a clip missing from MEDIAPRO.XML, an unreadable playlist or sidecar) are card-health notes to pass on.
- `inventory` reads clip facts with `ffprobe`. When `probed` is false the facts are **unknown**, not zero (ffprobe missing, or a file it cannot read).
- Two copies on the same physical drive are not a backup; say so if both destinations are on one volume.

## Not this skill's job

Editing or transcoding (video-editing-skill / ffmpeg-skill), proxies (ffmpeg-skill `proxy`), cloud upload (publish-skill), deleting or formatting the card, ASC MHL directory hashes / rename / flatten / collections (not yet), P2 / Canon XF / RED structures (listed as generic files).
