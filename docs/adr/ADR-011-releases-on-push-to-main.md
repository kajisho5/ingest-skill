# ADR-011: Releases on push to main: the version from pull request labels, the tag and the GitHub Release from one workflow

Status: Accepted

2026-11-19.

## Context

There was no release workflow and no tag: version 0.1.0 was never released, so dependents pinned this contract from main. The other skills (audio-production-skill, video-editing-skill, color-grading-skill, motion-graphics-skill, qc-skill, subtitle-skill, transcription-skill and more) release from one workflow on push to main, with the version decided by pull request labels.

## Decision

- **`.github/workflows/release.yml` runs on every push to main**, the same job as video-editing-skill's. `.github/scripts/compute_version.sh` decides the version: when `pyproject.toml` equals the latest `v*` tag, release-drafter (dry run, `.github/release-drafter.yml`) resolves the next one from the labels of the pull requests merged since that tag (`feature` / `minor` / `enhancement`: minor; `major` / `breaking`: major; anything else, or no label: patch). Otherwise the `pyproject.toml` version is released as written: the first release, or a version a pull request bumped by hand.
- **The release commit carries the version and the contract.** The job writes the version into `pyproject.toml`, `src/ingest_skill/__init__.py`, regenerates `contract/contract.json` with `python contract/generate.py`, fails unless `--check` passes and the file reports that version, prepends the commit subjects since the last tag to CHANGELOG.md, commits `chore(release): vX.Y.Z`, pushes an annotated tag and creates the GitHub Release with the same notes. It is one job because a tag pushed with the default token starts no other workflow.
- **Labels.** `.github/workflows/autolabel.yml` labels each pull request from its title, branch and files (`feature`, `fix`, `chore`, `documentation`). `major` / `breaking` is never applied from text, only by a person. A release-level label a bot put on a Dependabot pull request is removed before the version is resolved (`.github/scripts/dependabot_release_labels.py`).
- **Nothing goes to a package index.** The skill is installed from a git checkout.

## Consequences

- The first run (no tag yet) releases `pyproject.toml`'s version unchanged: `v0.1.0`, whose `contract/contract.json` is the one already on main (CI runs `generate.py --check`).
- A pull request no longer bumps the version or writes CHANGELOG.md by hand; it bumps `pyproject.toml` (and regenerates the contract file) only to choose a version the labels would not give. A major version is never chosen without a person's label.
- The tag's `contract/contract.json` is that tag's own contract: `git show vX.Y.Z:contract/contract.json`.
- `tests/test_release_scripts.py` (version decision and file bump, on throwaway repositories) and `tests/test_release_labels.py` (the Dependabot label rule and its wiring) cover the scripts.