# ADR-004: Read the Source Once for All Destinations (Single Read, Fan-Out Writes)

Status: Accepted

## Context

`ingest offload` copied the destinations one after another. For each file and each destination it read the source, hashed it and wrote the copy, then read the copy back. With two or three destinations, every byte of the card was read two or three times. `ECOSYSTEM_ROADMAP.md` step 5 asks for one read of the source per file, written to every destination. The bar is: "a test counts one read per file".

The situation this is for: a camera card offloaded on location to two or three drives. The card reader is usually the slowest device in the chain (a UHS-II SD reader gives roughly 150–300 MB/s, while USB / Thunderbolt SSDs write faster), so each extra pass over the card costs about one more card's worth of time. Re-reading also wears the card and its reader, which are the one copy that does not exist twice yet.

What must not change:

- **The read-back of every destination stays.** A copy counts as copied only when it was written to a temporary name, fsynced, renamed, then read back in full and every digest matched the digests taken from the source (README, SKILL.md, ADR-001). Only the source read is deduplicated.
- **The digests stay the same:** sha256 + c4 with `--hash sha256`, xxh64 with `--hash xxh64` (ADR-001), plus every earlier ASC MHL format that can be computed here, all taken in the same pass.
- Resume, `ingest-manifest.json`, the ASC MHL history, the exit codes and the contract fields stay as they are, except where item 5 below says why.
- Standard library only.

## Decision

1. **One reading thread, one writer thread per destination, bounded queues** (`core._fan_out`).
   - The calling thread opens the source once and reads it in `CHUNK` (8 MiB) pieces. It puts each chunk on every destination's queue, then hashes it (`MultiHash`, every algorithm in one pass).
   - Each destination has a `_Writer` thread. It writes the chunks it is given to the same temporary name as before (`.<name>.ingest-<pid>.part`), then flushes, fsyncs, copies the source's timestamps and renames the file into place.
   - The chunks are `bytes` objects shared by every queue, not copies. Each queue holds at most `QUEUE_DEPTH` = 4 chunks. Memory therefore stays at roughly (4 + 2) × 8 MiB, whatever the number of destinations.
   - After the read, every destination is read back and compared with the source digests: those just written, and those that already had the file (resume). This is the same full read-back as before, but each destination runs in its own thread (`_hash_each`), because destinations are normally separate drives.
   - `_copy_verified`, which carries a source's ASC MHL history (ADR-001 item 5), is the same path with one destination.
2. **Why threads and not a sequential chunk loop.**
   - A sequential loop (read a chunk, hash it, write it to destination 1, 2, 3, then read the next one) also reads the source once. But it does one thing at a time: each chunk costs read + hash + every write in turn. The card reader sits idle while the drives write, and the drives sit idle while the card is read.
   - With threads, the source read, the hashing and the writes to every destination overlap. A chunk's time is about the slowest of these, not their sum. File reads, writes and `fsync` release the GIL, and so does `hashlib` for buffers of this size, so threads give real overlap in CPython.
   - Other options were rejected:
     - `multiprocessing` would copy each 8 MiB chunk between processes.
     - `asyncio` has no asynchronous file I/O in the standard library.
     - Kernel copies (`os.sendfile`, `copy_file_range`) would hash different bytes from the ones they write, and need a second read to hash. They are also not available file-to-file on every OS.
   - The cost is a little more code. The queue handoff (`_give`) never waits on a writer thread that has died, so a writer that breaks cannot hang the run.
3. **A slow destination sets the pace; it cannot corrupt the others.**
   - Each byte must reach every destination, and the source is read only once. The read therefore waits when a destination's queue is full (backpressure). The run goes at the speed of the slowest destination. The other options are unbounded memory, or reading the source again for the slow drive, which is what this step removes.
   - A slow writer only delays. Every destination still receives the same chunks in the same order, and every destination is read back on its own. A test runs with a deliberately slow destination and a one-chunk queue.
4. **A destination that fails does not stop the others** (the field case: one drive fills up, or is unplugged).
   - An error while a destination's temporary file is opened, written, synced or renamed ends that file's copy **on that destination only**:
     - its writer keeps taking chunks and discards them, so the read never waits on it;
     - its temporary file is removed, and nothing is left at the destination path;
     - the file is `failed` on that destination (`failed` count, and a `detail` that names the destination path).
   - The other destinations go on writing. They are read back and verified as usual.
   - The run then continues with the next file on every destination, including the one that failed, as before. A transient error affects only one file. A full drive fails each later file quickly with its own error. Stopping every copy because one drive failed would leave a crew on location with no complete backup at all.
   - A read-back that does not match removes that destination's file and records `VERIFY_MISMATCH` for that destination only, as before.
   - The run is not ok, and exits 3, when any destination failed (unchanged).
   - **When the source fails, the file fails everywhere.** This covers a read error (a card pulled out) and a source that changed while it was read. Every temporary file of that file is removed, and no destination keeps a partial or torn copy.
5. **Each destination's manifest and ASC MHL list what that destination has** (a deliberate behaviour change).
   - Before, a file's overall status decided what went into the records: `failed` or `conflict` on any destination took the file out of *every* destination's `ingest-manifest.json` and ASC MHL generation, even where it had been copied and verified. Those good copies then showed up as `unlisted` in `verify`.
   - Now the status is kept per destination:
     - a destination lists, in its manifest and its MHL generation, every file it copied or found identical (verified);
     - a file that failed on a destination is not listed there as present.
   - **`ingest-manifest.json` gains an optional `"failed": [{"path", "size", "detail"}]`.** It lists the files this run could not copy to this destination. The schema stays `ingest/manifest@1`, because readers ignore unknown keys (the same reasoning as `"card"` in ADR-003). The key is written only when a file failed, so a clean run's manifest is unchanged. A later run that completes the copy rewrites the manifest without it.
   - **The ASC MHL generation has no record for a failed file.** ASC MHL has no way to say "not copied". Its `failed` action means "differs from an earlier record" (ADR-001 item 4), which is a different statement. The failure is therefore recorded in `ingest-manifest.json` and in the offload report, not in the MHL.
   - **`verify` reports an incomplete copy.** A path in the manifest's `"failed"` is listed under a new `copy_failed` key of the verify report, and as a failure with the existing code `INCOMPLETE` (exit 3), `against: ingest-manifest.json`. The report is then not ok. Before, a folder whose offload had failed on a file could verify as ok, because only the files that arrived were checked.
   - Conflicts are not listed under `"failed"`. The file at that path belongs to someone else, and the offload report still names it, as before.
   - In the offload report, `files[].status` stays the overall status (`failed` if any destination failed). `destinations[]` keeps the same counters. A file whose copy failed on a destination now still carries its source `hashes`, because the source was read in full. Before, those hashes were empty when the failed copy was the only one.
6. **SOURCE_CHANGED is detected without a second read.**
   - Before, a change in the source showed up only when two destinations' reads disagreed. By then, the first destination had already kept its copy.
   - Now the source's size and modification time (`st_mtime_ns`) are compared before and after its single read, and the number of bytes read must equal the size. Any difference is `SOURCE_CHANGED` for every destination, and no destination keeps the file.
   - A same-size, same-mtime rewrite of a file while it is being read cannot be seen this way. A camera card being offloaded is not being written, and every copy still matches, byte for byte, the bytes that were hashed (the read-back).

## Verification

- `tests/test_fanout.py` wraps `open()` (both `builtins.open` and `io.open`) to count every open, and every byte read, of every source file. Faults are injected through the same wrapper. The tests check:
  - **one open and one full read per source file**, with 1, 2 and 3 destinations, on a resume (every destination has every file), and on a mixed run (one new destination);
  - every destination is still opened once for read-back per file;
  - **byte-identical output** across three destinations, and the same manifest in each;
  - **the digests are unchanged**: each equals an independent `hashlib` / C4 / `xxhash` computation of the whole file, with 64 KiB chunks so that the clips are several chunks;
  - **one destination failing mid-copy** (`ENOSPC` after two chunks). The other two are complete, byte-identical, listed in their manifest and MHL, and `verify` ok. The failed one has no partial or temporary file, and its manifest lists the file under `"failed"`. Its `verify` is not ok (`copy_failed`, `INCOMPLETE`, exit 3). A resume writes only the missing file, reads the source once, and is ok;
  - a destination that cannot be opened;
  - a slow destination with a one-chunk queue;
  - a source read error, which fails the file everywhere and leaves nothing;
  - a source that grows while it is read, which gives `SOURCE_CHANGED` everywhere and is kept nowhere.
- The new tests fail on the previous implementation: for example, `DCIM/notes.txt` was opened 2 or 3 times.
- **Same output as before.** A deterministic card (a 9 MB clip of several chunks, a small clip, a text file and an empty file) was offloaded to three destinations with `--hash sha256` and `--hash xxh64`, then resumed, with the previous and the new code. The file statuses, every digest, the per-destination counters and every manifest entry (without timestamps) were identical.
- The existing 40 tests pass unchanged. CI runs the suite with the standard library only, and again with `.[xxhash]`, on 3 OSes × Python 3.9 / 3.11 / 3.12 (ADR-002).

### Benchmark note (indicative only, not a CI gate)

`benchmarks/fanout.py` offloads a synthetic 268 MB card (4 files) to 3 destinations. It reports the wall time and the bytes read from the source. `--reader-mbps` throttles reads of the source only, to model a card reader that is slower than the drives. The run here was one 4-core container, with every "drive" on the same virtual disk and a warm page cache. Real drives will differ; only the ratio of source bytes read is exact.

| `--hash` | Reader | Before: time, source read | After: time, source read |
|---|---|---|---|
| sha256 | unthrottled | 15.0 s, 3.00× | 4.2 s, 1.00× |
| sha256 | 160 MB/s | 17.9 s, 3.00× | 6.1 s, 1.00× |
| xxh64 | unthrottled | 2.8 s, 3.00× | 1.6 s, 1.00× |
| xxh64 | 160 MB/s | 9.7 s, 3.00× | 3.0 s, 1.00× |

With the throttled reader, the xxh64 run comes close to its floor: 268 MB at 160 MB/s is 1.7 s for one read, against 5.0 s for three. With sha256, the run is bound by hashing (SHA-256 and SHA-512 of every byte, ADR-001) and by the read-backs, which now run in parallel.

## Consequences

- An offload to N destinations reads the card once instead of N times. Its speed is set by the slowest of the card reader, the hashing and the slowest destination, instead of their sum over all destinations.
- The run uses up to N + 1 threads while it copies a file, and N threads while it reads back. Memory stays at about 50 MB, whatever N is.
- When two destinations are on the same physical drive, their writes and read-backs now compete for it at the same time. Two copies on one drive are still not a backup (SKILL.md).
- Consumers of `ingest-manifest.json` may meet the optional `"failed"` list. Consumers of the verify report may meet `copy_failed`. Both are additions: no key changed meaning, and no exit code changed.
- A per-destination manifest can now differ between destinations of the same run. That happens exactly when one of them failed or had a conflict.
