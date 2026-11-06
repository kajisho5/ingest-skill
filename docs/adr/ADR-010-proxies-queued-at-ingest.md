# ADR-010: Proxies Queued at Ingest (`offload --proxy`)

Status: Accepted

## Context

`ECOSYSTEM_ROADMAP.md` step 11: for same-day turnaround, light editing proxies should be ready on the laptop when the card offload ends. After a clip is verified, ffmpeg-skill `proxy` runs on it (with a hardware encoder when doctor reports one) while the next files copy. The proxy and its original are linked in the inventory by hash.

The bar: "an offload with `--proxy` gives one proxy per clip linked by the original's hash; a failed proxy does not fail the offload".

What ffmpeg-skill offers (3.x, read only):

- `scripts/proxy.py <input> -o <output> [--width N] [--hw] --json` makes a small H.264 proxy (640 px wide by default).
- `scripts/_contract.py doctor --json` reports, among other things:
  - `hw.platform_ok`: whether its `--hw` (VideoToolbox, Apple Silicon only) can run here;
  - `gpu_encoders`: the GPU encoders the ffmpeg build carries (NVENC, QSV, VAAPI, AMF, VideoToolbox);
  - its `version`.
- No ffmpeg-skill tool takes NVENC / QSV / VAAPI / AMF. `--hw` is VideoToolbox only, and the CPU (x264) is the default.

Ownership: proxies stay with ffmpeg-skill (`ecosystem.json`; this repository's contract said "proxy generation (ffmpeg-skill proxy)" under `not_provided`). Ingest decides only which files, when, from which copy, where the result goes, and how it is linked.

## Decision

1. **`offload --proxy [--ffmpeg-skill DIR] [--proxy-width PX]`.**
   - ffmpeg-skill is found at `--ffmpeg-skill`, else `$INGEST_FFMPEG_SKILL_DIR`, else `~/.claude/skills/ffmpeg-skill` (where `npx ffmpeg-skill` installs it; the agent uses the same default). The folder must hold `scripts/proxy.py`.
   - An explicit folder that is not ffmpeg-skill is not replaced by another one.
   - Without ffmpeg-skill, the run is refused as **`PROXY_UNAVAILABLE`** (exit 2) before anything is written, like `HASH_UNAVAILABLE`.
   - `ingest doctor` reports `ffmpeg-skill` as `ok` with its path, or `unavailable`. The contract lists it under `capabilities.optional` (kind `skill`).
2. **Which files.**
   - Every **video member** of the card's clips (cards.py, names only): `.mp4 .mov .mxf .mts .m2ts .avi .mkv .mpg .mpeg`.
   - The camera's own proxies (Sony `Sub/`) are not clips, and audio files have no picture. Neither gets one.
   - Blackmagic RAW, RED R3D and Canon Cinema RAW Light are listed as `skipped`, because ffmpeg does not decode them.
   - A spanned recording gets one proxy per member file. Joining them is ffmpeg-skill `join`'s work.
3. **When, and from which copy.**
   - When a file is recorded as copied or found identical on a destination, its first verified copy is queued.
   - One worker thread runs `python <ffmpeg-skill>/scripts/proxy.py <copy> -o <tmp> --json` for each queued file in turn, while the main thread copies the next files.
   - The proxy is made **from the verified copy, never from the card**. The card is read once (ADR-004), and the copy is the file that was verified.
   - The offload waits for the queue before it reports, so the proxies are ready when the command returns.
   - With `--follow` (ADR-007), a growing file is queued once it is sealed.
4. **Where, and how it is linked.**
   - Output goes to `<destination>/ingest-proxies/<path>.proxy.mp4`, in the destination whose copy was used: one proxy per clip, not one per destination. It is written to a temporary name and renamed once ffmpeg-skill exits 0 with a non-empty file.
   - `ingest-proxies/proxies.json` (`ingest/proxies@1`) records each proxy:
     - `original`, and `original_hash` (the offload hash from the manifest: algorithm and value);
     - `path`, `size` and `sha256` of the proxy;
     - `encoder`, `made_by` (ffmpeg-skill, `proxy`, its version), `args` (with `<original>` / `<proxy>` placeholders) and `created_at`.
   - `inventory` gives every clip an `ingest_proxies` list: the index entries whose `original_hash` equals the hash the manifest records for one of its members, with whether the proxy file is there. The link is the hash, so a proxy follows its original's content, not its name.
   - The top-level `ingest-proxies/` folder belongs to the copy, like `ascmhl/`: `scan` skips it (`skipped_proxies`). The proxies are therefore not clips, not `unlisted` in `verify`, and not carried by a later offload of that copy to another drive. They can be made again.
5. **A proxy never fails the offload.**
   - A non-zero exit, a missing or empty output, or an exception in the worker is recorded as `failed`, with ffmpeg-skill's last error line, and as a warning on that destination.
   - `ok` and the exit code depend only on the copies, as before.
   - A resume does not encode again a proxy whose index entry has the same original hash and whose file still has the recorded sha256 (`exists`).
6. **Hardware encoding.**
   - ffmpeg-skill's doctor runs once per offload. When it reports `hw.platform_ok`, `--hw` is passed and ffmpeg-skill encodes on VideoToolbox. Its own JSON says whether the GPU was used in the end (`hw.used`), and that is what each item's `encoder` records.
   - Otherwise the CPU is used. The report says why (`encoder_detail`), and lists the build's `gpu_encoders` as information only, because no ffmpeg-skill tool can use NVENC / QSV / VAAPI / AMF today. That gap is ffmpeg-skill's to close; it is stated, not worked around.
7. **The report** gains `proxy`:
   - `skill_dir`, `version`, `encoder`, `encoder_detail` and `gpu_encoders`;
   - counts of `made`, `exists`, `failed` and `skipped`;
   - `items`, one per file.

   `not_provided` now says "encoding proxies itself": the encoder is ffmpeg-skill's.

## Verification

`tests/test_proxy.py` replaces ffmpeg-skill with a stand-in folder that has the same two entry points. Its `proxy.py` logs its arguments and start time, and fails on a file named `*_BROKEN*`. Its `_contract.py doctor --json` reports `hw.platform_ok` from an environment variable. With it:

- **One proxy per clip, linked by hash:**
  - A card with three video clips (one broken), an audio file and a BRAW clip, offloaded to two destinations, is `ok`: 2 made, 1 failed (with ffmpeg-skill's error, and a warning), 1 skipped (BRAW).
  - The proxies were made from the first destination's verified copies and exist only there.
  - `proxies.json` records each original's sha256 (equal to an independent hash of the card file) and the proxy's sha256.
  - `inventory` links each clip to its proxy through the manifest hash. The proxies are not clips, `verify` is ok with nothing unlisted, and the broken clip has no link.
- **While the next files copy:** with the second clip's copy slowed down, the first clip's proxy starts before the second clip is done.
- **Hardware:** when the stand-in's doctor reports `hw.platform_ok`, every run gets `--hw` and the items record `hw`. Otherwise none does.
- A resume keeps the proxies already made and tries only the broken one again.
- `--proxy` without ffmpeg-skill is `PROXY_UNAVAILABLE` and writes nothing. Without `--proxy`, nothing is queued.
- With ffmpeg-skill and ffmpeg installed (`INGEST_TEST_FFMPEG_SKILL_DIR`; CI has neither), one test runs the real `scripts/proxy.py` on a generated clip. It was run once on ffmpeg-skill 3.1.8 (CPU: no VideoToolbox in the container) and made a 640 px H.264 proxy.
- VideoToolbox itself was not exercised: no Apple Silicon machine was available. The `--hw` path is checked only through the stand-in.

## Consequences

- At the end of an offload with `--proxy`, an editor has light proxies next to the verified copy, and the inventory says which proxy belongs to which clip.
- The proxy encodes compete with the hashing for the CPU (a single worker, so at most one encode at a time). The offload's wall time can grow by the encode time of the last clips.
- New error code `PROXY_UNAVAILABLE` (exit 2), optional capability `ffmpeg-skill`, report key `proxy`, inventory key `ingest_proxies`, scan status `skipped_proxies`. All additive.
