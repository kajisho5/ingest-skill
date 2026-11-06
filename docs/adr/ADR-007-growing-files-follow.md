# ADR-007: Recordings Still Being Written (Growing Files): `offload --follow`

Status: Accepted

## Context

`ECOSYSTEM_ROADMAP.md` step 8 (field review 2026-11-05): an all-day conference is recorded on a recorder or a capture station, and the editor wants to start cutting at each break with what has been recorded so far. Until now, a file that changes while it is read fails as `SOURCE_CHANGED` on every destination (ADR-004 item 6), so a recording in progress could not be offloaded at all.

The bar: "a fixture file appended in three bursts ends with one verified copy and one MHL entry; a copy taken mid-growth is never sealed".

What must not change: the source is only read; nothing is overwritten that ingest did not write; a copy counts as copied only after a full read-back matched the source digests; the ASC MHL records only verified files; ADR-004's single read and per-destination failure. Without `--follow`, a file that changes while it is read is still `SOURCE_CHANGED`.

This is about files being recorded to a disk or share that ingest can read (a recorder's own drive, a capture station, a network share). Live streaming and playout are out of scope.

## Decision

1. **`offload --follow [--settle S] [--poll P] [--follow-for T]`** polls the source every `P` seconds (default 2).
   - A file whose modification time is older than `S` seconds (default 30) is first only looked at. If its size and mtime are the same at the next poll, it is copied the ordinary way (ADR-004: one read, every destination read back, resume, conflicts).
   - A file that changed between two polls, or was modified less than `S` seconds ago, is **growing**.
   - The run ends when every file is sealed (or failed / a conflict), after `T` seconds, or on Ctrl-C. A file that appears later (the recorder starts a new one) is picked up at the next poll while the run lasts.
2. **A growing file is copied as it grows: appended bytes only** (`follow.grow`).
   - Each pass reads the source once from the last verified offset to its current size. The bytes are hashed into a running hash of the whole file and appended, through one writer thread per destination, **in place** at the destination path, then flushed and fsynced. In place, so that the partial copy can already be opened in an editor, which is the point of the step.
   - The appended range of every destination is then read back and its hash compared with the hash of the bytes read from the source. Only then does the verified offset advance.
   - A destination that fails to write, or whose read-back differs, is cut back to its last verified size and leaves this file's copy (ADR-004 item 4). The others go on.
   - A source that becomes shorter than what was copied is `SOURCE_CHANGED`; every destination keeps its verified bytes.
3. **Until it is sealed, the copy is listed as growing, never as a file.**
   - Each destination's `ingest-manifest.json` gets an optional `"growing": [{"path", "size", "<hash>", "mtime", "growing": true}]`: the verified size and the hash of exactly those bytes. It is rewritten after every poll. The schema stays `ingest/manifest@1`; readers ignore the key, as with `"failed"` (ADR-004).
   - A growing copy is never under `"files"` and never in the ASC MHL: neither can say "partial".
   - The offload report lists the file with status `growing` and no hashes, counts it in `destinations[].growing`, and is not ok (exit 3), because the copy is not complete. The report's `follow` key gives the settle window, the polls, why the run stopped (`all_sealed`, `follow_for`, `interrupted`) and, per growing file, the passes, the verified bytes and whether it was sealed.
   - `verify` checks the verified bytes of a growing copy against the manifest hash. A match is listed under a new `growing` key, with an `INCOMPLETE` failure (exit 3). A mismatch is `changed`. `inventory` marks the clip `"growing": true`.
4. **Sealing** (`follow.seal`), once the file's size and mtime have stayed unchanged for `S` seconds:
   - If the source was read in more than one pass, it is re-hashed in full once, and that hash must equal the running hash. A recorder can rewrite bytes it wrote earlier (an MP4 / MOV / MXF header updated when the file is closed); appended-bytes-only copying would miss that. When the hashes differ, the partial copies are removed (they are ingest's own) and the file is copied again in full, the ordinary way. The detail says so. If the source changed during the re-hash, the file is not stable after all and the copy goes on growing.
   - If the whole file was read in one pass with its size and mtime unchanged before and after, there is no second read (ADR-004's assumption).
   - Every destination is then read back in full with every digest (the offload hash, the ASC MHL format and earlier formats), as for any copy. The destination gets the source's mtime. The file then goes into `"files"` and the run's ASC MHL generation, once.
5. **A partial copy is resumed, never overwritten by mistake.**
   - A destination file listed under its manifest's `"growing"` and at least as long as the verified size is ingest's own partial copy. Bytes beyond the verified size (an interrupted pass) are cut off. The next `offload --follow`, or a plain `offload` once the recording has ended, reads the source from the start once (to rebuild the running hash), appends what is missing, and reads back the whole range. If the earlier bytes do not match the source, it is a conflict: the partial copy is left at its verified size.
   - Any other existing file at the destination path is a conflict, as before, and is never appended to.
   - A plain `offload` that finds a partial copy and a source still changing leaves the file `growing` (`SOURCE_CHANGED`, "continue it with --follow").
6. **What is not detected:** whether the recorder still holds the file open. There is no portable way (no API on macOS or Windows, nothing on a network share), so the settle window is the only end-of-recording signal. A recorder that pauses for longer than `S` seconds and then appends to the same file will find its sealed copy a conflict at the next run; raise `--settle` for such a recorder. Some file systems update the size or mtime of a file being written late (exFAT / SMB caching); an old-looking file is therefore copied only after two looks a poll apart, and the ordinary copy's before / after check (ADR-004 item 6) still applies.

## Verification

`tests/test_follow.py` simulates the recorder: the test's `sleep()` appends a burst to the source between polls, and its `clock()` advances by the poll interval, so no test waits for a real settle window.

- **Three bursts:** one copy per destination, byte-identical to the final file; one entry in `"files"`, none under `"growing"`; one ASC MHL entry; `verify` ok. Four passes (the first look plus one per burst). The source bytes read are the file's size once while it grew, plus one full re-hash at the seal (counted by wrapping `open()`, as in ADR-004). A finished file next to it is read once.
- **Mid-growth:** a run stopped by `--follow-for` while the file is still written. The copy is under `"growing"` with `"growing": true`, equals the source's first bytes and their hash, is not in `"files"` and not in the ASC MHL; `verify` is not ok (`growing`, `INCOMPLETE`, exit 3); `inventory` marks the clip growing. The next `--follow` run continues it from the verified size, reads the source once, seals it, and the history has exactly one ASC MHL entry for it.
- A plain `offload` completes a partial copy left by `--follow`.
- A header rewritten after the first pass is found at the seal and the file is copied again in full.
- A different file at a destination path is a conflict, left untouched, while the other destination completes.
- CLI flags, and `FOLLOW_INVALID` (exit 2, nothing written) for a non-positive `--poll`.

## Consequences

- During a break, `ingest offload <recorder> --dest ... --follow --follow-for 60` leaves verified partial copies an editor can open. A later run (or one left running) seals them once the recording stops.
- A file followed over several passes is read about twice (once as it grows, once to seal). The destinations are read about twice too (each appended range, then in full at the seal).
- Consumers of the manifest may meet the optional `"growing"`, and consumers of the verify report the `growing` key. Both are additions; no key changed meaning.
- New error code `FOLLOW_INVALID` (exit 2).
