# ADR-002: Test Python 3.9, 3.11 and 3.12 in CI, and Run 3.9 on ubuntu-24.04

Status: Accepted

## Context

`pyproject.toml` declares `requires-python = ">=3.9"`, and the README promises "Python 3.9+, standard library only". CI ran only Python 3.12, so nothing checked the floor. `ECOSYSTEM_ROADMAP.md` step 2 asks for CI on Python 3.9, 3.11 and 3.12.

Python 3.9 reached end of life in October 2025. `actions/setup-python` has no 3.9 build for Ubuntu 26.04, which `ubuntu-latest` now resolves to. The first CI run of this change failed on that job with "The version '3.9' with architecture 'x64' was not found for Ubuntu 26.04". `macos-latest` and `windows-latest` still provide 3.9.

## Decision

1. **The test matrix is OS × Python version**: `ubuntu-latest`, `macos-latest` and `windows-latest`, each with `3.9`, `3.11` and `3.12`. That makes nine jobs.
   - Every job runs the suite twice, as before: with the standard library only (xxh64 must be refused as `HASH_UNAVAILABLE`), then with `.[xxhash]`.
2. **The Ubuntu 3.9 job runs on `ubuntu-24.04`**, through a matrix `exclude` / `include` pair. The other Ubuntu jobs stay on `ubuntu-latest`.
   - The job is named `test (ubuntu-24.04, 3.9)`.
   - When `ubuntu-24.04` is retired, or 3.9 is dropped, this pin goes with it.
3. **`requires-python` stays `>=3.9`.** The floor is now tested on every OS. Raising it would be a separate decision, recorded in its own ADR.
   - The product code needed no change. The suite already passed on 3.9: every module uses `from __future__ import annotations` and the `typing` generics, and no 3.10+ syntax or standard-library API.

## Verification

- Locally, before pushing: the suite (stdlib only, then with `.[xxhash]`), `ingest contract --json` and `ingest doctor --json` on CPython 3.9, 3.11 and 3.12. All passed, with the two xxhash tests skipped in the stdlib-only run, as designed.
- CI: all nine jobs green on the pull request.

## Consequences

- CI takes nine jobs instead of three.
- A change that uses 3.10+ syntax or API (for example `X | None` evaluated at runtime, `match`, `zip(strict=True)`) now fails CI instead of breaking users on 3.9.
