# ADR-014: The 1.0 Compatibility Promise

2026-11-21. Status: Accepted (Velisa ADR-031 / D46; the owner approved the 1.0 transition).

## Context

Velisa ADR-031 sets what 1.0 means for a Skill. From 1.0.0, a change that `si contract-diff` calls breaking, to a part of the contract the promise covers, ships only in a major release. The promise covers:

- the `provides[]` entries marked `STABLE`, and the tools they map to;
- their request, response and error shapes;
- the top-level contract fields that are not tied to a capability.

An `EXPERIMENTAL` entry may still change in a minor release, with a CHANGELOG line. ingest-skill is sixth in ADR-031's order.

ADR-031's readiness measurements for this repository, checked again on 2026-11-21:

- **Breaking changes:** none in the tag diffs (v0.1.0 to v0.1.3) or in the `contract:*` labels of the merged pull requests.
- **Open roadmap steps:** none. Steps 1 to 11 are ticked.
- **Known defects:** none. There is no open issue and no open pull request.
- **Releases:** ADR-031 counted four (v0.1.0 to v0.1.3, criterion 4 asks for five). 1.0.0 is the fifth. Criterion 4 orders the Skills; it is not a gate. The code behind the three `STABLE` tools is older than the tags. It shipped from 0.1.0 (2026-10-30) and the release flow was added later (#22).
- **Agent:** video-production-agent pins this contract (`integration/siblings.lock.json`) and calls `offload`, `inventory` and `verify` (its ADR-066) and `fetch` (its ADR-059). The re-pin proves the release end to end.

The contract has a `lifecycle` field only on `provides[]` entries. This repository has four: `ingest.offload`, `ingest.verify`, `ingest.inventory` and `ingest.fetch`. Several newer features are options of `offload` and `inventory`, not entries of their own. A parameter has no lifecycle field, so ADR-031 section 3 records a parameter's status in text: the README, the CHANGELOG and this ADR.

## Decision

### 1. `STABLE` entries

- **`ingest.offload`** (`ingest/offload`). It covers:
  - the source read once and written to every destination (ADR-004), through a temporary file, fsync and rename;
  - the read-back of each copy by `--hash sha256` (the default) or `--hash xxh64` (ADR-001);
  - never overwriting, resume of identical files, conflicts reported;
  - `--project`, `--name-by folder|reel`, `--reel` and `--dry-run`;
  - `ingest-manifest.json` (`ingest/manifest@1`) and the ASC MHL history generation (`ascmhl/`, process `transfer`) in each destination;
  - the report (`ingest/offload-report@1`) and the exit codes 0, 2 and 3.
- **`ingest.verify`** (`ingest/verify`). The re-hash of a copy against its manifest and its ASC MHL history, the chain check, and the report (`ingest/verify-report@1`). It writes nothing.
- **`ingest.inventory`** (`ingest/inventory`), for one folder. It covers:
  - `ingest/inventory@2`: one clip per recording;
  - the card structures Sony XDCAM / XAVC, XDCAM EX, AVCHD, Blackmagic RAW and generic (ADR-003), with spanned files in order;
  - the camera sidecar fields, the reel / card id and its precedence, and the manifest hash;
  - the ffprobe facts. Their values are what ffprobe reports, and they are unknown without it.

These three are the original operation of this skill (0.1.0), and video-production-agent's card offload calls them with exactly these options. They run locally and need only the standard library. `xxhash` is an optional Python package, and ffprobe is an optional binary. ASC MHL and C4 follow published specifications (ASC MHL v1.0, SMPTE ST 2114). The tests check them against the reference implementation's vectors.

A breaking change to anything listed above, or to the error codes and the top-level contract fields, ships only in a major release (2.0.0).

### 2. Experimental parts of the `STABLE` tools

These options and fields of `offload` and `inventory` stay experimental. Each may still change in a minor release, with a CHANGELOG line:

- **`offload --follow`** (ADR-007, roadmap step 8): growing files, `--settle` / `--poll` / `--follow-for`, the manifest's `growing` key and the report's `follow` key. It was added on 2026-11-06. There is no portable way to tell whether a recorder still holds a file open, so the settle window may still be reshaped.
- **`offload --progress`** and the slow-destination warning (ADR-009, step 10): the JSON-lines events on stderr and `destinations[].warnings`. They were added on 2026-11-06. Its thresholds (50% of the median, after 64 MB) come from simulated drives only.
- **`offload --proxy`** (ADR-010, step 11): `--ffmpeg-skill` / `--proxy-width`, `ingest-proxies/proxies.json` (`ingest/proxies@1`), the report's `proxy` key and the inventory's `ingest_proxies`. It runs another Skill (ffmpeg-skill `proxy`), so its behaviour follows that engine. The hardware path has not been exercised.
- **Sync facts and sync groups** (ADR-008, step 9, amended by ADR-013 on 2026-11-20): each clip's `sync`, `inventory` with several folders, and `sync_groups` (`ingest/inventory-set@1`). ADR-013 changed how timecode and rates are read one release before 1.0. LTC is detected, never decoded.

A request with none of these options gets the `STABLE` behaviour of section 1. These parts are left out of the promise because they are newer or depend on another engine, not because they are known to be wrong.

### 3. `EXPERIMENTAL` entry

- **`ingest.fetch`** (ADR-005, ADR-006). It downloads through yt-dlp, a third-party library, from third-party sites. Its extractors, the format ids and the failure modes change with yt-dlp releases and with the sites, which this repository does not control. It is the only tool that uses the network. The offload it runs after the download is the `STABLE` one, and the `fetch` record in the manifest stays experimental.

### 4. How the promise is kept

- Promoting an entry (or a part of section 2) to `STABLE` is metadata only. si's `lifecycle-promoted` rule classifies it as `none`, so it can ship in any release.
- **Deprecation** of a `STABLE` entry or option (ADR-031 section 3):
  1. mark it `DEPRECATED` in a minor release, naming its replacement in the README and the CHANGELOG;
  2. keep it working for at least one more minor release and 30 days;
  3. remove it only in the next major release.
- **Breaking-change review.** `major-label-check.yml` fails a `contract:breaking` pull request on 1.x without the `major` label. For a break only in `ingest.fetch`, ADR-031 section 5 lets the reviewer remove `contract:breaking` by hand. si reports an option of a `STABLE` tool as part of that tool. So a break in a section 2 part counts as a break of `offload` or `inventory` until that part has an entry of its own. The reviewer can remove the label only when both of these hold:
  - every breaking item in the contract-diff comment is in `ingest.fetch` or a part listed in section 2;
  - the pull request and the CHANGELOG entry name it.

### 5. Release

The 1.0 pull request carries the `major` label. release-drafter then resolves 0.1.3 → 1.0.0, and `release_gate.py` treats `major` as release-level. No workflow changes. The contract diff of the pull request is `none` (three `lifecycle-promoted` items). `contract_version` stays `1.0`: it is a separate axis from the release version.

## Consequences

- A caller that uses only the section 1 options can float across 1.x minors. video-production-agent's card offload is such a caller.
- A caller that uses `fetch` or any section 2 part reads the CHANGELOG on each minor release.
- video-production-agent's re-pin sees a 0.x → 1.0.0 release with a `none` diff and widens its gate to `1.0.`.
