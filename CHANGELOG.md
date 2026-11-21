# Changelog

## v1.0.0 - 2026-11-21

- feat!: 1.0.0 - offload, verify and inventory are STABLE; breaking changes to them only in a major release (ADR-014) (#30) (e03f743)
- ci: a contract:breaking pull request on 1.x needs the major label (Velisa ADR-031 section 5) (#29) (2455772)
- ci: release gate leaves contract:* labels out of the count (#28) (5f3afc9)

## v0.1.3 - 2026-11-20

- fix(sync): rates and timecode by velisa/timebase@1; refuse labels that do not exist (#27) (c7f8b42)
- ci: Velisa's reusable changelog-check/release-guard; dependencies PRs release a patch (D36, D37) (#25) (27bb92c)

## v0.1.2 - 2026-11-19

- ci(release): test the release commit before pushing; no release for chore/ci/docs-only changes (#24) (f11d0d6)

## v0.1.1 - 2026-11-19

- ci: run contract-diff on every pull request (#23) (cd5c3df)

## v0.1.0 - 2026-11-19

- ci: release on push to main (version from PR labels, tag and GitHub Release) (#22) (33f4b9d)
- ci: call Velisa's contract-diff on contract changes, one run per pull request (#21) (828c5c3)
- ci: commit the contract as contract/contract.json (#20) (4e850d3)
- test(progress): time simulated drives on their own clock (fixes red main) (#19) (38f2c29)
- offload --proxy: ffmpeg-skill proxy queued on verified clips (roadmap step 11, ADR-010) (#18) (542ec11)
- offload --progress: speed per drive, ETA, slow-destination warning (roadmap step 10, ADR-009) (#17) (3698159)
- inventory: sync facts and multicam sync groups (roadmap step 9, ADR-008) (#16) (dce9143)
- offload --follow: recordings still being written (roadmap step 8, ADR-007) (#15) (afba10f)
- docs(roadmap): field review steps 8-11 (2026-11-05) (#14) (e9d5bf9)
- docs(roadmap): bring status and wiring up to date (2026-11-05 review) (#13) (cb1ddb3)
- docs: tick roadmap step 6 (agent adapter + SkillSpec media_offload) (#12) (b1be752)
- CI: check out the OS as kajisho5/Velisa (renamed from AI-video-production-OS) (#11) (edd9818)
- deps: raise the yt-dlp floor of the fetch extra, per Python version (#10) (9a9ff29)
- ingest fetch <url>: download through yt-dlp, then offload like a card (roadmap step 7) (#9) (7a4301c)
- Read the source once for all destinations: single read, fan-out writes (roadmap step 5) (#8) (22054ba)
- Card-structure-aware inventory: spanned clips, camera sidecars, reel naming (roadmap step 4) (#7) (5963c2b)
- CI on Python 3.9 / 3.11 / 3.12 (roadmap step 2) (#6) (e61c3dc)
- feat: ASC MHL history per destination + optional xxHash64 (roadmap step 3) (#5) (2dda5ec)
- fix: contract in the OS registry shape (#4) (5d3b680)
- docs: add app icon and wordmark (made with fresnel) (#3) (83f39c3)
- docs: URL download is owned here (owner decision) (#2) (f1b5282)
- feat: ingest-skill 0.1.0 - verified offload, verify and clip inventory (#1) (3710c4f)
- Initial commit (a28686e)
