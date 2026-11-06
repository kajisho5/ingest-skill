# ADR-008: Sync Facts and Multicam Sets in the Inventory

Status: Accepted

## Context

`ECOSYSTEM_ROADMAP.md` step 9: per clip, the start timecode, the frame rate, whether an audio channel carries LTC (detected, not decoded), the recorder and camera ids and the wall-clock start; clips from different cards whose times overlap are grouped as a `sync_group` candidate for ffmpeg-skill `multicam` / `sync`. The aim: material from the cameras and from a separate audio recorder arrives already grouped for multicam editing.

The bar: "a fixture with two cameras and one recorder over the same 10 minutes gives one sync group with three members".

What ffmpeg-skill offers (3.x, read only): `scripts/multicam.py` and `scripts/sync.py` align recordings by audio cross-correlation, with the reference first and a `--max-offset` search window (default 30 s). They take file paths. Neither reads timecode or wall clocks to propose which files belong together, and nothing in ffmpeg-skill detects LTC. So the grouping and the LTC check are done here; the actual alignment stays with ffmpeg-skill.

What must not change: inventory reads only; a fact that no source gives is unknown (null), never guessed; the standard library is enough (ffprobe and ffmpeg are optional); the single-folder output stays `ingest/inventory@2`, so the agent's adapter (step 6) is unaffected.

## Decision

1. **Every inventory clip gets a `sync` object** (`sync.clip_sync`). Each fact names its source under `sync.sources`.

   | Fact | Sources, in order |
   |---|---|
   | `frame_rate` (`"25/1"`, `"30000/1001"`) | ffprobe's `avg_frame_rate` of the video stream; the Sony NonRealTimeMeta `formatFps` (`"59.94i"` is fields: 29.97 frames) |
   | `timecode_rate` | Sony `LtcChangeTable@tcFps`; iXML `SPEED/TIMECODE_RATE`; else the frame rate |
   | `start_timecode` | ffprobe's `timecode` tag (MOV `tmcd`, MXF); the first Sony `LtcChange` (frame 0); for a BWF file, its `bext` time reference written as a non-drop label at the timecode rate |
   | `start_tod_s` | seconds since midnight: the start timecode at the timecode rate (drop-frame labels counted back to real frames), or the BWF `bext` TimeReference ÷ sample rate |
   | `wall_clock_start` (`value`, `utc`, `local`, `source`) | the Sony `CreationDate` (with its UTC offset, so both `utc` and `local`); the BWF `bext` OriginationDate + OriginationTime (local, no zone: `utc` null); ffprobe's `creation_time` (UTC only: cameras disagree on whether it is local or UTC, start or end, so `local` stays null) |
   | `duration_s` | ffprobe; Sony `Duration` frames ÷ frame rate; the WAV data chunk ÷ byte rate |
   | `device` | `kind` (`recorder` for an audio-only file, else `camera`); Sony `Device` (manufacturer, model, serial); BWF `bext` Originator and OriginatorReference; iXML `TAPE`; ffprobe's `make` / `model` tags |
   | `ltc` | `checked`, and the channels that carry LTC as `{"stream", "channel"}` (1-based channel) |

   - The `bext` and `iXML` chunks of a WAV / BWF file are read here with the standard library (EBU Tech 3285 field offsets; iXML refused if it carries a DTD or entity, like the camera XML of ADR-003).
   - A `bext` TimeReference of 0 is read as unknown. It is what recorders write when no timecode is set; a recording that really starts at exactly midnight loses its time of day.
   - The Sony `LtcChange@value` is 8 hex digits: the frames, seconds, minutes and hours bytes in that order, BCD, with the SMPTE ST 12-1 flag bits masked (bit 6 of the frames byte marks drop frame). This order matches the repository's own fixture and is consistent with the LTC frame. It was not checked against a file from a real card, so the raw value stays in `metadata.ltc_start` next to the decoded label.
2. **LTC is detected from the signal, never decoded** (`sync.detect_ltc`). The first 4 s of each channel are read. LTC is a biphase-mark square wave of 80 bits per frame. Its zero crossings (with hysteresis at a quarter of the peak) fall at one bit period (a 0 bit) or half a bit period (a 1 bit). A channel is LTC when:
   - it has 1,700–5,200 crossings per second;
   - the long-interval cluster gives 21–33 frames per second;
   - at least 90 % of the intervals are within the long or the short cluster, and each cluster holds at least 10 %. The sync word has twelve 1 bits in a row, so real LTC always has both.

   A 1 kHz tone (one interval only), a square wave, noise and silence are not LTC. Below 16 kHz sampling, nothing is claimed. PCM WAV files (16, 24 or 32-bit integer) are read with the standard library. For the audio inside other files, each audio stream is decoded to 16-bit PCM at 48 kHz with `ffmpeg` when it is on PATH, reading 4 s and writing nothing. Without ffmpeg, `ltc.checked` is false with the reason. `doctor` reports `ffmpeg` as `degraded`, and the contract lists it under `capabilities.optional`.
3. **`ingest inventory <folder> <folder> ...` gives an inventory set** (`ingest/inventory-set@1`): each folder's inventory under `cards`, and `sync_groups`.
   - Two clips of **different** cards are linked when their intervals (start + duration) overlap by at least 1 s on a shared basis. The bases, in order of preference:
     - `timecode`: time of day, for jam-synced devices;
     - `utc`;
     - `local`: the devices' clocks set to the same zone.
   - Links are transitive (a recorder take that spans two camera clips joins them). A group needs clips from at least two cards.
   - Each group gives:
     - `basis`: the first basis every member has, else `mixed`;
     - `members`: card reel, clip, absolute path, `kind`, duration, `offset_s` from the reference on that basis, and `reference`, which is the longest camera clip;
     - `span_s`, `common_overlap_s` and `span_start`;
     - `next`: the call to ffmpeg-skill `scripts/multicam.py`, reference first, `--offsets-only`, with `--max-offset` and `--analyze-seconds` widened to cover the clock offsets.
   - The detail says what the group is: the clocks only make it a candidate, and multicam / sync measure the real offsets from the audio.
   - Refusals: the same folder twice, or one inside another, is `DUPLICATE_SOURCE` (exit 2). `--reel` with several folders is `REEL_INVALID`.
4. **Not done:** decoding LTC (its value comes from the container or the sidecar, not the audio); midnight wrap of time-of-day timecode; clock drift; grouping clips of the same card with each other; reading timecode from AVCHD / Blackmagic RAW sidecars (still not interpreted, ADR-003).

## Verification

`tests/test_sync.py` builds the cards: Sony XAVC cards with `M01.XML` sidecars (creation date with a UTC offset, duration in frames, LTC start, device), and a recorder card holding a Broadcast Wave file (`bext` + `iXML`, 10 min 2 s at 1 kHz). LTC is synthesised as SMPTE ST 12-1 biphase-mark audio.

- **Two cameras and a recorder over the same 10 minutes**, plus a later clip on camera A, give one sync group with three members: one per card; basis `timecode`; offsets 0 / +2 / −2 s; 598 s of common overlap; the reference first in the multicam call. The later clip is not in the group. Without timecode (time reference 0, no LTC table), the local wall clocks give the same group with basis `local`.
- Cards that do not overlap give no group.
- The per-clip facts: timecode, time of day, frame and timecode rates, duration, device ids and the wall clock (UTC and local for the camera, local only for the recorder).
- LTC is found on channel 2 of a stereo BWF (a voice-like signal on channel 1). The detector accepts 23.976, 24, 25, 29.97 and 30 fps and rejects a tone, a square wave, noise, silence and an 8 kHz file.
- With ffmpeg and ffprobe on PATH (skipped otherwise; CI has neither), LTC is found inside a MOV, and its `tmcd` start timecode comes from ffprobe.
- Unreadable files give unknown facts, not zeros.
- The CLI set output and the refusals.
- The suite also passes with neither ffprobe nor ffmpeg on PATH.

## Consequences

- The agent, or an editor, can hand each `sync_group` to ffmpeg-skill `multicam` / `sync` without picking the files by hand.
- The inventory of a folder reads the first seconds of every WAV, and spawns ffmpeg once per audio stream of other clips when ffmpeg is installed.
- `metadata` of a Sony clip gains `ltc_start` (raw). Each clip gains `sync`. Both are additions to `ingest/inventory@2`.
- New error code `DUPLICATE_SOURCE` (exit 2) and optional capability `ffmpeg`.
