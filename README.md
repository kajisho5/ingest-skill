<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/app-icon-dark.png">
    <img src="brand/app-icon.png" width="128" alt="ingest-skill app icon">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/logo/ingest-skill-wordmark-white.svg">
    <img src="brand/logo/ingest-skill-wordmark-black.svg" height="48" alt="ingest-skill">
  </picture>
</p>

<p align="center"><b>Verified media offload from cards and recorders</b></p>

Verified media offload for camera cards and recorders: every copy is checked by a sha256 (or xxh64) read-back, nothing is ever overwritten, an interrupted offload resumes, every destination carries an ASC MHL history, and a backup can be re-verified at any time. Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS) ecosystem (Ingest department), and usable on its own.

```bash
pip install -e .
ingest offload /Volumes/CARD_A --dest /Volumes/SSD1/Footage --dest /Volumes/SSD2/Footage --project 2026-10-30_event
ingest verify  /Volumes/SSD1/Footage/2026-10-30_event/CARD_A
pip install -e ".[xxhash]"   # optional: --hash xxh64
ingest offload /Volumes/CARD_B --dest /Volumes/SSD1/Footage --project 2026-10-30_event --hash xxh64
ingest inventory /Volumes/SSD1/Footage/2026-10-30_event/CARD_A --json
ingest offload /Volumes/Untitled --dest /Volumes/SSD1/Footage --project 2026-10-30_event --name-by reel   # -> .../A001
```

- **offload**: copies every file of the card folder to each destination (`<dest>/<project>/<card name>/...`), through a temporary file, fsync and rename, then reads it back and compares the hashes (`--hash sha256`, the default, or `--hash xxh64`). Writes `ingest-manifest.json` and appends a generation to an **ASC MHL** history (`ascmhl/`, process `transfer`; c4 with sha256, xxh64 with xxh64) in each destination. A source that already has an `ascmhl/` history keeps it: the copy continues that chain. Identical existing files are skipped (resume); different ones are reported as conflicts and never overwritten.
- **verify**: re-hashes a destination against its manifest (sha256 or xxh64) and its ASC MHL history, which is checked against its chain: missing, changed and unlisted files, each failure with a code, the expected and the actual hash. Works from the ASC MHL history alone, including one written by another ASC MHL tool. Writes nothing.
- **inventory**: one entry per recording, from the card's structure: Sony XDCAM / XAVC (`XDROOT`, `M4ROOT`, `MEDIAPRO.XML` + `Clip/`), XDCAM EX (`BPAV`), AVCHD (`PRIVATE/AVCHD/BDMV`), Blackmagic RAW, and a generic fallback. A recording split across files (an XDCAM EX take, an AVCHD playlist recording) is one clip with its member files in order; proxies, sidecars and index files are listed with their clip, not as clips. What the camera's `M01.XML` / `MEDIAPRO.XML` reliably says (UMID, creation date, duration in frames, codec, frame size, device and serial) is included, nothing guessed. Each clip has its size and manifest hash, and, when `ffprobe` is installed, duration, codecs, resolution, frame rate, timecode and creation time.
- **reel / card id**: `--reel`, else the reel recorded by the offload, else the camera ID + reel number every clip name starts with (Sony `A001C001_…`, Blackmagic `A001_…`), else a deterministic `CARD-<fingerprint>`. `offload --name-by reel` names each destination's card folder with it. The offload still copies every file of the card unchanged: grouping is reporting only.
- **contract / doctor**: machine-readable declaration and environment check for agents.

Exit codes: `0` ok, `2` refused (nothing written), `3` incomplete. Python 3.9+, standard library only; `xxhash` is an optional extra (`pip install "ingest-skill[xxhash]"`; without it `--hash xxh64` is refused as `HASH_UNAVAILABLE` and `ingest doctor` reports it unavailable) and `ffprobe` is optional (inventory facts are reported as unknown without it). No network access. See [SKILL.md](SKILL.md) for the agent-facing rules and [docs/adr](docs/adr) for design decisions.

Not yet: ASC MHL directory hashes, rename, flatten and collections; recording a verify run as an ASC MHL generation; parallel destinations; Panasonic P2 / Canon XF / RED card structures (listed file by file); a recording continued on another card.
