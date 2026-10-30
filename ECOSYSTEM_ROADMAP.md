# Roadmap — ingest-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **取込 (Ingest)**, status `scaffold`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

v0.1.0 on main, SKILL.md on the default branch. Python 3.9+ stdlib (`xxhash` optional). `ingest offload | verify | inventory | contract | doctor`; sha256 or xxh64 read-back, never overwrites, resumable, manifest and ASC MHL history per destination; card-structure-aware inventory (one clip per recording, camera sidecars, reel / card id, ADR-003); the source read once for every destination (ADR-004). 47 tests, CI on 3 OSes × Python 3.9 / 3.11 / 3.12 (3.9 on ubuntu-24.04, see ADR-002), contract validated against the OS schema in CI.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| 素材取り込み(検証付きコピー) | done |  |
| 二重バックアップ | done | the source is read once per file and written to every destination in parallel; each destination read back and verified on its own; a failed drive does not stop the others (ADR-004) |
| 素材台帳 | done | one clip per recording (Sony XDCAM / XAVC, XDCAM EX, AVCHD, BRAW, generic), camera sidecars, reel / card id, ffprobe facts; P2 / Canon XF / RED listed file by file |
| URL素材取得 | missing | owned here since 2026-10-30 (owner decision); the agent's yt-dlp call (`agent/ingest.py`) moves here |

## Wiring

No adapter in the agent. Calls ffprobe directly; proxies stay with ffmpeg-skill `proxy`.

## Steps

- [x] **1. Contract fits the OS schema** — `skill` as an object (or drop it), ffprobe under capabilities.optional, SOURCE_CHANGED in ERRORS, a jsonschema test against the OS schema (the jsonschema test landed with step 4, [#7](https://github.com/kajisho5/ingest-skill/pull/7))
  - Done when: contract validates against registry/capability_contract.schema.json in CI
- [x] **2. Merge to main; CI on Python 3.9 / 3.11 / 3.12** ([#6](https://github.com/kajisho5/ingest-skill/pull/6))
  - Done when: CI green; SKILL.md on the default branch (marketplace install shows 1 skill)
- [x] **3. ASC MHL output + xxHash64** — MHL per destination; `verify` accepts sha256 or xxh64 ([#5](https://github.com/kajisho5/ingest-skill/pull/5))
  - Done when: MHL round-trips; verify passes on both hash kinds
- [x] **4. Card-structure-aware inventory** — group spanned clips (XDCAM/AVCHD/BRAW/etc.), read camera sidecars, reel/card naming ([#7](https://github.com/kajisho5/ingest-skill/pull/7))
  - Done when: fixture card trees give one clip per recording
- [x] **5. Read the source once for all destinations** — single read, fan-out writes ([#8](https://github.com/kajisho5/ingest-skill/pull/8), ADR-004)
  - Done when: a test counts one read per file
- [ ] **6. Agent adapter + SkillSpec media_offload** — inventory becomes OBSERVED observations on new assets
  - Done when: `video-agent plan <card>` offloads then analyses, with provenance
- [ ] **7. `ingest fetch <url>`** — URL download through yt-dlp (optional dependency, reported by doctor), the same verify + manifest + inventory as a card, source URL and extractor version recorded
  - Done when: a fixture URL (local HTTP server) lands with a manifest; the agent calls ingest-skill instead of yt-dlp

## Boundaries

URL download (URL素材取得) is owned here since 2026-10-30 (owner decision); until step 7 ships, the agent keeps its own yt-dlp call.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
