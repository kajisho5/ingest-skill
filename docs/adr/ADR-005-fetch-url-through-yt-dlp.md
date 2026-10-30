# ADR-005: `ingest fetch <url>`: Download Through yt-dlp, Then Offload Like a Card

Status: Accepted

## Context

URL download (URL素材取得) belongs to this repository since 2026-10-30 (owner decision, `ECOSYSTEM_ROADMAP.md`). Until now, video-production-agent downloaded URLs itself, in `src/video_agent/agent/ingest.py`:

- it runs the `yt-dlp` executable (found through `VIDEO_AGENT_YTDLP` or `PATH`) as a subprocess, with an argument list (no shell);
- it first runs `--dump-json` and reads yt-dlp's own `is_live` / `was_live`, then downloads with `-f bestvideo*+bestaudio/best --merge-output-format mp4`, adding `--live-from-start` only for a stream that is still live;
- the file goes to `<workspace>/downloads/<id>.<ext>`, with no read-back and no second copy. It records the URL as given, the page URL, the id, the title, the sha256, the yt-dlp version and the live flags.

Roadmap step 7 asks for `ingest fetch <url>`: yt-dlp as an optional dependency that `doctor` reports, "the same verify + manifest + inventory as a card", and the source URL and extractor version recorded. The bar is: "a fixture URL (local HTTP server) lands with a manifest".

What a download adds to an offload, and what it must not change:

- A URL can carry credentials: `user:password@`, signed query parameters (`token`, `sig`, `X-Amz-Signature`, …) and fragment tokens (`#access_token=`). The record must not keep them.
- yt-dlp's metadata holds the signed media URL it downloaded from and the request headers, including cookies. None of it may be stored.
- Offload, verify and inventory stay local and standard-library only (README). A missing extra refuses cleanly, as `HASH_UNAVAILABLE` does for xxh64 (ADR-001).

**Sources** (read on 2026-10-30): the yt-dlp README, "Embedding YT-DLP" and "Embedding examples"; the parameter list in `yt_dlp/YoutubeDL.py` (yt-dlp 2026.10.26, `cookiefile`, `cookiesfrombrowser`, `usenetrc`, `allow_unplayable_formats`, `noplaylist`, `live_from_start`, `logger`, `socket_timeout`, `merge_output_format`); `yt_dlp/options.py`, where configuration files are loaded (only by the command line's option parser).

## Decision

1. **yt-dlp is an optional extra, called as a library.** `pip install "ingest-skill[fetch]"` adds yt-dlp (`>=2024.8.6` when this ADR was written; now `>=2026.7.4`, or `>=2025.10.14` on Python 3.9, see ADR-006). `fetch` calls `yt_dlp.YoutubeDL(params).extract_info(url, …)`.
   - **Why a library and not a subprocess:**
     - There is no command line at all. The URL is one Python string handed to yt-dlp, never interpolated into a shell or parsed as an option. A subprocess also avoids the shell, but a URL that starts with `-` then needs a `--` separator, and every option is a string to get right.
     - What `fetch` needs to record comes back as a structured value: `extractor`, `format_id`, `requested_downloads[].filepath`, `is_live`. A subprocess would have to parse `--dump-json` / `--print` output.
     - The extra is a Python package, like `xxhash`: "installed" means importable in the environment that runs `ingest`, and `doctor` checks the same thing `fetch` uses. With a subprocess, `doctor` would check a binary on `PATH` that may not be the one the package pins.
     - The library does not read yt-dlp configuration files. Those are loaded only by yt-dlp's own command-line parser. A user's `~/.config/yt-dlp/config` with `--cookies-from-browser` therefore cannot slip cookies into `fetch`.
     - Tests replace the module (`sys.modules["yt_dlp"]`) with a fake, with no executable to install on `PATH` and no Windows `.cmd` launcher.
   - **What the library costs:** yt-dlp runs inside the `ingest` process, and there is no overall timeout. `socket_timeout` is 60 s, so a server that stops answering fails the download. A live stream runs until it ends. Ctrl-C stops it. yt-dlp still runs ffmpeg as its own subprocess to merge streams.
   - Without the extra, `fetch` is refused as **`FETCH_UNAVAILABLE`** (exit 2) after the URL and destination checks and before anything is written or downloaded. The message names the install command. `ingest doctor` reports `checks["yt-dlp"]` as `unavailable` (`version: null`), or `ok` with the yt-dlp version and whether ffmpeg is on `PATH`. `ok` stays true either way. The contract lists `yt-dlp` under `capabilities.optional`.
2. **The download goes to a staging folder; the copy is an ordinary offload.**
   - Staging is a new `ingest-fetch-*` folder in the system temporary folder, or under `--staging DIR`. yt-dlp writes `<staging>/_download/<id>.<ext>`.
   - The download folder is renamed to the card name, `--name` or `<extractor>-<video id>` made safe (the `--reel` rule of ADR-003, else `FETCH-<8 hex of the redacted URL's sha256>`). That folder goes through `offload` unchanged:
     - one read, writes to every destination, read-back of each;
     - `ingest-manifest.json` and an ASC MHL generation (process `transfer`: the staging-to-destination copy is what is verified);
     - never an overwrite: a destination that already has a different file is a conflict.
   - So `ingest verify` and `ingest inventory` work on `<dest>/<project>/<name>` exactly as on a card.
   - The staging folder is removed when every destination is verified. Otherwise it is **kept**, and the report names it (`staging.kept`, `staging.path`), so a download is never lost.
   - The download is accepted only when yt-dlp left at least one file, no `.part` / `.ytdl` remains, and every file yt-dlp reported is inside the staging folder. Otherwise the error is **`FETCH_FAILED`** (exit 3, retryable), and nothing is written to any destination.
3. **What yt-dlp is asked to do matches the agent.**
   - It first extracts metadata only (`download=False`). It adds `live_from_start` only when yt-dlp itself says `is_live`, never from the shape of the URL.
   - Format: `bestvideo*+bestaudio/best` merged into `mp4` when ffmpeg is on `PATH` (the agent's selector, and yt-dlp's own default). Without ffmpeg it is `best`, one file that already has both streams, with a warning, because nothing could be merged. `--format` overrides it.
   - `noplaylist`. A playlist or multi-video page is refused (`FETCH_FAILED`): `fetch` takes the URL of one video.
   - Stated, not left to defaults: `cookiefile` and `cookiesfrombrowser` are `None`, `usenetrc` is false, no info JSON, thumbnail or subtitle file is written, and `allow_unplayable_formats` is false.
   - yt-dlp's output goes to a logger, so `--json` stays the only thing on stdout. Warnings are kept, redacted, in the report.
4. **What is recorded**: the `"fetch"` object (`ingest/fetch@1`), in the fetch report, in each destination's `ingest-manifest.json`, and in `ingest inventory` (`"fetch": null` for a card).

   | Key | Value |
   |---|---|
   | `source_url` | the URL as given, redacted (item 5) |
   | `webpage_url` | yt-dlp's page URL, redacted |
   | `redacted` | the names (never the values) of what was removed: `userinfo`, query parameter names, `#name` for the fragment, `;name` for path parameters |
   | `extractor`, `extractor_key` | yt-dlp's extractor (e.g. `youtube` / `Youtube`, `generic` / `Generic`) |
   | `downloader` | `{"name": "yt-dlp", "version": …}`. yt-dlp's extractors ship inside yt-dlp and have no version of their own: the yt-dlp version **is** the extractor version |
   | `format_selector`, `format_id` | the selector used, and the format yt-dlp picked (e.g. `137+140`) |
   | `id`, `title`, `duration` | as yt-dlp reported them |
   | `is_live`, `was_live`, `live_from_start` | yt-dlp's live flags, and whether the capture started at the stream's beginning |
   | `files` | the downloaded files, relative to the card folder |
   | `started_at`, `fetched_at` | UTC, when the download started and when it finished |

   The manifest's existing `source` key is the staging folder the copy was made from (removed after a verified run). The origin of the material is in `fetch`.

   **Never recorded:** cookies, request headers, the signed media URL yt-dlp downloaded from, and credential values in either URL. The manifest schema stays `ingest/manifest@1` and the inventory stays `ingest/inventory@2`, because readers ignore unknown keys (as for `"card"` in ADR-003 and `"failed"` in ADR-004).
5. **Redaction rules** (`fetch.redact_url`). Over-redacting a harmless parameter is the accepted cost.
   - `user:password@` in the host part is removed.
   - A query, fragment (`#a=b&…`) or path (`;name=value`) parameter whose value is non-empty has its value replaced by `REDACTED` when its name, case-insensitively, contains `token`, `sig`, `key`, `secret`, `passw`, `auth`, `credential`, `session`, `cookie`, `jwt`, `hmac` or `policy`, or is `pass`, `pwd`, `code`, `sid`, `st`, `hdnts`, `hdnea` or `otp`. This covers the AWS (`X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`), CloudFront (`Signature`, `Policy`, `Key-Pair-Id`), Akamai (`hdnts`, `hdnea`) and OAuth (`access_token`, `code`) forms.
   - Everything else is kept byte for byte, such as `v=`, `t=` and `list=`.
   - Every URL inside an error or warning message is redacted the same way before it is reported.
   - **Limit:** a secret inside a plain path segment (`/token/abc123/clip.mp4`) cannot be told from a name, so it is kept. Do not fetch such a URL when the record must stay clean.
6. **Rights and DRM.** README and SKILL.md say the user must have the right to download what they fetch. No option bypasses DRM (`allow_unplayable_formats` is never exposed), and no login, cookie or `.netrc` option exists. A protected or login-only video is refused by yt-dlp and reported as `FETCH_FAILED`. All of this is in the contract's `not_provided`.
7. **New error codes:** `URL_INVALID` (not one `http`/`https` URL with a host; exit 2), `NAME_INVALID` (exit 2), `FETCH_UNAVAILABLE` (exit 2), `FETCH_FAILED` (exit 3, retryable). Every refusal happens before a destination is written. The contract's `execution.network` is now `true`, with `network_tools: ["ingest/fetch"]`, and each tool declares its own `network`. Offload, verify and inventory never touch the network.

## Verification

- `tests/test_fetch.py`. No test reaches the internet.
  - A fake `yt_dlp` module writes a small local file. With two destinations, the tests check:
    - the copies are byte-identical, the manifest and MHL are written, and `verify` and `inventory` pass;
    - a flipped byte fails `verify` with `VERIFY_MISMATCH`;
    - the manifest's `"fetch"` equals the report's;
    - no secret value appears anywhere in the report or the manifests (userinfo, query and fragment tokens, the CDN signature and the cookie the fake returns);
    - yt-dlp received the original URL, `noplaylist`, no cookie options and no `allow_unplayable_formats`.
  - Further cases:
    - the live branch (`live_from_start` only when `is_live`);
    - the missing extra: `FETCH_UNAVAILABLE`, nothing written and no staging left, CLI exit 2, `doctor` `unavailable` then `ok` with the version;
    - the refusals before any download (`URL_INVALID` for `ftp:`, `file:`, an option-shaped string; `NO_DESTINATION`, `DUPLICATE_DESTINATION`, `NAME_INVALID`, a destination around the staging folder);
    - `FETCH_FAILED` with a redacted message for a download error, a playlist, no file and a partial file;
    - a conflict on one destination, which keeps the staging folder while the other destination verifies.
  - Unit tests of the redaction rules, including AWS, CloudFront, path parameters and harmless parameters left alone.
  - With the extra installed (CI's second run): the **real yt-dlp** downloads a fixture file from an HTTP server on 127.0.0.1, which is the roadmap's "fixture URL (local HTTP server)". The URL carries userinfo and a token. Both destinations verify, and the manifest has neither secret. A missing file on that server is `FETCH_FAILED`.
  - `INGEST_FETCH_NETWORK_URL=<url>` runs one real internet download. It is skipped by default.
- CI runs the suite with the standard library only (fake yt-dlp, `FETCH_UNAVAILABLE` checked), then with `.[xxhash,fetch]` (the loopback test), on 3 OSes × Python 3.9 / 3.11 / 3.12 (ADR-002). yt-dlp 2026.10.26 needs Python 3.11 or later; on 3.9, pip installs the newest yt-dlp release that supports 3.9 (2025.10.14, which still has published advisories: ADR-006).

## Consequences

- The skill is no longer "no network": `fetch` is its one network tool, and it is declared as such. Everything else is unchanged.
- A fetched folder is an ordinary offloaded folder. A re-run of the same fetch into the same destination resumes when the site serves identical bytes. When the bytes differ (another format, a new remux), it is a conflict and nothing is overwritten. Use another `--name` to keep both.
- Handover: video-production-agent can replace its own yt-dlp call with `ingest fetch <url> --dest <workspace>/downloads --json`. The fields it records today map onto the `"fetch"` record and the report (`files[].hashes.sha256`, `downloader.version`, `id`, `title`, `duration`, the live flags). Its unredacted `source_url` becomes the redacted one.
