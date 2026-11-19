# ADR-012: The release job tests its commit before pushing it; chore / ci / docs / test pull requests release nothing

2026-11-19. Amends ADR-011. The ecosystem rule is Velisa ADR-023 (decision D37).

## Context

The release commit is pushed with the default token, so no workflow runs on it on main. Two other skills broke main this way today (sound-design-skill, hyperframes-skill). Also, any merged pull request released a patch, a `ci:` change included.

## Decision

- The release job commits and tags locally, runs `python -m unittest discover -s tests` on that commit (step "Test the release commit before pushing it"), and only then pushes. A failure pushes nothing.
- A push to main releases only when something merged since the latest tag is releasable. `.github/scripts/release_gate.py` decides this, and `compute_version.sh` reads its answer as `RELEASABLE`. A pull request with a release-level label releases (`feature` / `minor` / `enhancement`, `fix` / `bug` / `patch`, `major` / `breaking`). So does a pull request with no label, as a patch. A pull request labelled only `chore`, `ci`, `documentation`, `docs`, `test`, `maintenance` or similar releases nothing. This is imagemagick-skill's rule. release-drafter's `default` accepts only major, minor or patch, so the gate runs first.
- The CHANGELOG heading form stays `## vX.Y.Z - date`; `tests/test_release_scripts.py` checks it.

## Consequences

- A version bumped by hand in a pull request is released whatever its labels.
- A release that fails the tests is not made; a later push retries.
