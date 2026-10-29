---
name: ingest-skill
description: Offload camera cards and recorders safely - copy a card folder to one or more drives with a sha256 read-back of every file, never overwrite, resume an interrupted copy, re-verify a backup later and list the clips (duration, codecs, resolution, timecode). Use when asked to ingest, offload, back up or verify footage, or to make a clip list of a shoot. Local only, no network.
---

# ingest-skill

Run `ingest <command> ... --json` (installed by `pip install -e .`). Every command prints one JSON document.

| Task | Command |
|---|---|
| Copy a card to two drives | `ingest offload /Volumes/CARD_A --dest /Volumes/SSD1/Footage --dest /Volumes/SSD2/Footage --project 2026-10-30_event --json` |
| See what would be copied | add `--dry-run` (writes nothing) |
| Re-check a backup | `ingest verify /Volumes/SSD1/Footage/2026-10-30_event/CARD_A --json` |
| Clip list | `ingest inventory /Volumes/SSD1/Footage/2026-10-30_event/CARD_A --json` |
| What this skill declares | `ingest contract --json`, `ingest doctor --json` |

## What it guarantees

- The source card is only read. It is never modified, deleted or formatted.
- Each copy is written to a temporary name, flushed to disk, renamed, then read back; it counts as copied only when the read-back sha256 equals the hash taken while reading the source.
- An existing file at the destination is never overwritten: identical content is skipped (so a re-run resumes an interrupted offload), different content is a **conflict** and the run is incomplete (exit 3).
- Each destination gets `ingest-manifest.json` (path, size, sha256, mtime of every file). `verify` re-hashes against it and reports missing, changed and unlisted files.
- Card housekeeping (`.DS_Store`, `._*`, `.Spotlight-V100`, `System Volume Information`, ...) and symbolic links are listed as skipped, never copied or followed.
- A destination inside the source (or the other way round), the same destination twice, or no destination is refused before anything is written (exit 2).

Exit codes: `0` ok, `2` refused, `3` incomplete (conflict, failure, missing or changed file).

## Report it honestly

- Say "verified" only when `ok` is true. If `ok` is false, name the conflicts / failures from the JSON; do not call the backup done.
- `inventory` reads clip facts with `ffprobe`. When `probed` is false the facts are **unknown**, not zero (ffprobe missing, or a file it cannot read).
- Two copies on the same physical drive are not a backup; say so if both destinations are on one volume.

## Not this skill's job

Editing or transcoding (video-editing-skill / ffmpeg-skill), proxies (ffmpeg-skill `proxy`), cloud upload (publish-skill), deleting or formatting the card, MHL / ASC MHL output and xxHash (not yet).
