# ADR-009: Progress, Speed and ETA of an Offload

Status: Accepted

## Context

`ECOSYSTEM_ROADMAP.md` step 10: on location, the crew needs to know how many minutes an offload has left, and which drive is slow, while the copy runs. `--progress` should write JSON lines (file, bytes, MB/s per destination, ETA), and a destination much slower than the others should be warned about during the copy.

The bar: "a fixture run emits monotonic progress, and a throttled destination raises the warning".

What constrains it:

- With `--json`, stdout is one report document. Agents parse it, so it must stay that way.
- The source is read once and written to every destination in parallel. The read waits for the slowest destination (ADR-004 item 3). Every destination therefore advances at the **same** pace, and a wall-clock rate per destination would not tell them apart.

## Decision

1. **Each destination's writer measures its own drive.**
   - The writer thread adds the bytes of every write and the time spent in it, then the time of the flush and `fsync`, to that destination's `DestStats` (`progress.py`).
   - The destination's speed is bytes ÷ busy time: how fast the drive accepts data when it is given some. That differs between a fast and a slow drive even though both finish together.
   - Every run reports it as `destinations[].bytes_written` and `destinations[].write_mb_s` (MB = 10⁶ bytes). `write_mb_s` is null when nothing was written, as on a resume.
2. **A slow destination is warned about while the copy runs.**
   - After each chunk, each destination that has written at least 64 MB (`SLOW_MIN_BYTES`, so the first, cached writes do not decide) is compared with the **median** speed of all such destinations. One under half the median is slow.
   - The median, not the fastest: two equal drives that differ by chance are never told apart. With two destinations, the median is their mean, so a warning means under a third of the other's speed.
   - The warning is given once per destination. It is a `slow_destination` event, and a line in `destinations[].warnings` naming the speeds and what to check (drive, port, hub, cable).
   - It never fails the run: every copy is still verified. It runs in every offload, with or without `--progress`.
3. **`offload --progress` writes JSON lines to stderr**, one event per line, flushed:

   | Event | When | Fields |
   |---|---|---|
   | `start` | before the first file | |
   | `progress` | after a chunk of the source is read, at most every 0.5 s | `file`, `file_bytes`, `file_size`, `destinations[]` (`root`, `bytes_written`, `mb_s`) |
   | `file` | a file is done | `file`, `file_size`, `status`, `destinations[]` |
   | `slow_destination` | item 2 | `root`, `dest_mb_s`, `median_mb_s`, `detail` |
   | `done` | after the manifests and the ASC MHL | `ok`, `destinations[]` |

   Every event also carries `elapsed_s`, `bytes_done`, `bytes_total`, `files_done`, `files_total`, `mb_s` (source bytes read ÷ elapsed) and `eta_s` (bytes left ÷ that rate).
   - `bytes_done` counts the source bytes read, which is also the work of a resume (identical files are read to be hashed).
   - A file that was not read in full (a failed read, a conflict) counts its remaining bytes when it is done. So `bytes_done` **only grows**, and ends at `bytes_total` with `eta_s` 0.
   - The rate includes the read-backs, because they are part of the elapsed time. The ETA is therefore the time the whole offload has left, not only the copying.
   - With `--follow` (ADR-007), `eta_s` is null: the end depends on the recording.
4. **Programmatic use:** `offload(..., progress=callable)` receives each event as a dict. `progress_interval_s` and `slow_min_bytes` can be lowered (tests).
5. **Not measured:** the speed of the source (the card reader) and of the read-backs, and the appended passes of a growing file under `--follow`.

## Verification

`tests/test_progress.py`. Chunks are 64 KiB, and a drive of a given speed is simulated by delaying every write to it (the `open()` wrapper of `tests/test_fanout.py`).

- **Monotonic progress:**
  - `bytes_done`, `files_done` and `elapsed_s` never decrease;
  - the run ends at `bytes_total`, three files done, `eta_s` 0;
  - one `progress` line per chunk with a zero interval, each with the file's bytes and every destination;
  - one `file` line per file;
  - `bytes_written` and `write_mb_s` per destination.
- **Throttled destination:** three destinations at about 6.5, 1.3 and 6.5 MB/s. A single `slow_destination` event names the slow one, before the first (1.2 MB) file is done, and its warning is in that destination's `warnings` only. The run is ok.
- Two equally slow destinations raise no warning.
- A resume reads and counts every byte but writes none (`write_mb_s` null).
- `--follow` has no ETA.
- The CLI writes the events to stderr and one report to stdout.

## Consequences

- An operator, or the agent, can show "N minutes left" and name a slow drive without waiting for the end of the offload.
- Timing a write costs two clock reads per 8 MiB chunk.
- The offload report gains `bytes_written` and `write_mb_s` per destination, and possibly a slow-destination warning. These are additions.
