# ADR-006: Raise the yt-dlp Floor, per Python Version

Status: Accepted

## Context

A supply-chain audit (Trail of Bits supply-chain-risk-auditor, checked against OSV on 2026-10-30) found that the `fetch` extra's floor, `yt-dlp>=2024.8.6` (ADR-005), allows releases with six published advisories, five of them HIGH:

| Advisory | Severity | Fixed in |
|---|---|---|
| GHSA-g3gw-q23r-pgqm (`--netrc-cmd` command injection) | HIGH | 2026.02.21 |
| GHSA-69qj-pvh9-c5wg (`--exec` command injection) | HIGH | 2026.6.9 |
| GHSA-c6mh-fpjc-4pr3 (dangerous file type via filename sanitization) | HIGH | 2026.6.9 |
| GHSA-vx4q-3cr2-7cg2 (code execution via manifest downloads with aria2c) | HIGH | 2026.6.9 |
| GHSA-f7j3-774f-rfhj (cookie leak with the curl downloader) | MODERATE | 2026.6.9 |
| GHSA-6v4j-43gg-vj32 (downstream command injection via output sanitization) | HIGH | 2026.7.4 |

`fetch` never sets `--exec`, `netrc_cmd`, cookies or an external downloader (ADR-005, item 3), so several of these are not reachable through it. The filename and output sanitization advisories are, because `fetch` writes the file yt-dlp names, and the floor should not admit any of them.

yt-dlp 2026.7.4 is the first release with none of them, and it requires Python 3.10. Every yt-dlp release since 2025.10.22 does. This skill declares `requires-python = ">=3.9"` and tests 3.9 on every OS (ADR-002).

## Decision

1. **Python 3.10 and later: `yt-dlp>=2026.7.4`.**
2. **Python 3.9: `yt-dlp>=2025.10.14`**, the last yt-dlp release that installs on 3.9, through an environment marker. All six advisories above remain open there. Python 3.9 is not dropped to raise a dependency floor: raising `requires-python` is its own decision (ADR-002, item 3).
3. `ingest doctor` is unchanged. It already reports the installed yt-dlp version (`checks["yt-dlp"].version`), and it never reported a minimum.

## Verification

- `pip install -e ".[xxhash,fetch]"` resolves to yt-dlp 2025.10.14 on Python 3.9 and to a release at or above 2026.7.4 on 3.11 and 3.12. The suite, including the real-yt-dlp loopback download of ADR-005, passes on each, locally and in CI.
- Each version was checked against OSV: 2026.7.4 has no advisories, and 2025.10.14 has the six above.

## Consequences

- Users on Python 3.9 who use `fetch` keep a yt-dlp with known HIGH advisories. Use Python 3.10 or later for `fetch` when that matters.
- When Python 3.9 is dropped (ADR-002), the second line of the extra goes with it.
