# ingest-skill

Verified media offload for camera cards and recorders: every copy is checked by a sha256 read-back, nothing is ever overwritten, an interrupted offload resumes, and a backup can be re-verified at any time. Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS) ecosystem (Ingest department), and usable on its own.

```bash
pip install -e .
ingest offload /Volumes/CARD_A --dest /Volumes/SSD1/Footage --dest /Volumes/SSD2/Footage --project 2026-10-30_event
ingest verify  /Volumes/SSD1/Footage/2026-10-30_event/CARD_A
ingest inventory /Volumes/SSD1/Footage/2026-10-30_event/CARD_A --json
```

- **offload**: copies every file of the card folder to each destination (`<dest>/<project>/<card name>/...`), through a temporary file, fsync and rename, then reads it back and compares sha256. Writes `ingest-manifest.json` in each destination. Identical existing files are skipped (resume); different ones are reported as conflicts and never overwritten.
- **verify**: re-hashes a destination against its manifest: missing, changed and unlisted files.
- **inventory**: clip list with size, manifest hash and, when `ffprobe` is installed, duration, codecs, resolution, frame rate, timecode and creation time.
- **contract / doctor**: machine-readable declaration and environment check for agents.

Exit codes: `0` ok, `2` refused (nothing written), `3` incomplete. Python 3.9+, standard library only; `ffprobe` is optional (inventory facts are reported as unknown without it). No network access. See [SKILL.md](SKILL.md) for the agent-facing rules.

Not yet: MHL / ASC MHL manifests, xxHash, parallel destinations.
