"""Machine-readable contract (`ingest contract --json`), in the shape the AI Video Production OS registry reads
(skill_id, version, contract_version, provides, dependencies, not_provided)."""
from __future__ import annotations

from typing import Any, Dict

from . import __version__

SKILL_ID = "ingest"
CONTRACT_VERSION = "1.0"

TOOLS = [
    {"tool_id": "ingest/offload", "role": "execution", "writes_media": True, "mutates_input": False, "deterministic": True, "network": False,
     "description": "Copy every file of a card or recorder folder to one or more destinations (the source is read once per file, written to every destination in parallel), verified by a sha256 (default) or xxh64 read-back of each; writes ingest-manifest.json and an ASC MHL history generation (ascmhl/) in each. With --follow, recordings still being written are copied as they grow (appended bytes only, each range read back), listed as growing partial copies, and sealed once unchanged for the settle window",
     "argv": ["offload", "<source>", "--dest", "<dir>", "[--dest <dir> ...]", "[--project <name>]", "[--hash sha256|xxh64]", "[--name-by folder|reel]", "[--reel <id>]",
              "[--follow [--settle <s>] [--poll <s>] [--follow-for <s>]]", "[--progress]", "[--proxy [--ffmpeg-skill <dir>] [--proxy-width <px>]]", "[--dry-run]", "--json"]},
    {"tool_id": "ingest/verify", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True, "network": False,
     "description": "Re-hash an offloaded folder against its manifest (sha256 or xxh64) and its ASC MHL history: missing, changed and unlisted files; writes nothing",
     "argv": ["verify", "<folder-or-manifest>", "--json"]},
    {"tool_id": "ingest/inventory", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True, "network": False,
     "description": "Clip list of a card or an offloaded folder, one entry per recording: card structure (Sony XDCAM / XAVC, XDCAM EX, AVCHD, Blackmagic RAW, generic), spanned files grouped in order, camera sidecar metadata, reel / card id, manifest hash, ffprobe facts and sync facts (start timecode, frame rate, LTC on an audio channel, camera / recorder ids, wall-clock start). Given several folders, also the sync-group candidates: clips of different cards whose times overlap, for ffmpeg-skill multicam / sync",
     "argv": ["inventory", "<folder>", "[<folder> ...]", "[--reel <id>]", "--json"]},
    {"tool_id": "ingest/fetch", "role": "execution", "writes_media": True, "mutates_input": False, "deterministic": False, "network": True,
     "description": "Download one video URL with yt-dlp (optional extra) into a staging folder, then offload it to every destination exactly like a card (verified copies, ingest-manifest.json, ASC MHL history); the manifest records the source URL (credentials redacted), the extractor, the yt-dlp version, the format id and the time",
     "argv": ["fetch", "<url>", "--dest", "<dir>", "[--dest <dir> ...]", "[--project <name>]", "[--name <folder>]", "[--hash sha256|xxh64]", "[--format <yt-dlp selector>]", "[--staging <dir>]", "--json"]},
]

ERRORS = {
    "SOURCE_NOT_FOUND": {"exit_code": 2, "retryable": False},
    "NO_DESTINATION": {"exit_code": 2, "retryable": False},
    "DUPLICATE_DESTINATION": {"exit_code": 2, "retryable": False},
    "DESTINATION_OVERLAPS_SOURCE": {"exit_code": 2, "retryable": False},
    "MANIFEST_NOT_FOUND": {"exit_code": 2, "retryable": False},
    "MANIFEST_INVALID": {"exit_code": 2, "retryable": False},
    "HASH_UNAVAILABLE": {"exit_code": 2, "retryable": False},
    "REEL_INVALID": {"exit_code": 2, "retryable": False},
    "FOLLOW_INVALID": {"exit_code": 2, "retryable": False},
    "DUPLICATE_SOURCE": {"exit_code": 2, "retryable": False},
    "PROXY_UNAVAILABLE": {"exit_code": 2, "retryable": False},
    # ingest fetch (ADR-005): refusals before anything is written or downloaded, then a failed download.
    "URL_INVALID": {"exit_code": 2, "retryable": False},
    "NAME_INVALID": {"exit_code": 2, "retryable": False},
    "FETCH_UNAVAILABLE": {"exit_code": 2, "retryable": False},
    "FETCH_FAILED": {"exit_code": 3, "retryable": True},
    "INCOMPLETE": {"exit_code": 3, "retryable": True},
    # Per-file failures: recorded in that file's `detail`, and the run ends INCOMPLETE.
    "SOURCE_CHANGED": {"exit_code": 3, "retryable": True},
    "VERIFY_MISMATCH": {"exit_code": 3, "retryable": True},
    "FILE_MISSING": {"exit_code": 3, "retryable": True},
    "MHL_INVALID": {"exit_code": 3, "retryable": False},
}


def contract() -> Dict[str, Any]:
    return {
        "skill_id": SKILL_ID,
        "id": SKILL_ID,
        "skill": {"id": SKILL_ID, "package": "ingest-skill", "version": __version__},
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
        "provides": [{"id": "ingest.offload", "lifecycle": "EXPERIMENTAL", "tool_id": "ingest/offload"},
                     {"id": "ingest.verify", "lifecycle": "EXPERIMENTAL", "tool_id": "ingest/verify"},
                     {"id": "ingest.inventory", "lifecycle": "EXPERIMENTAL", "tool_id": "ingest/inventory"},
                     {"id": "ingest.fetch", "lifecycle": "EXPERIMENTAL", "tool_id": "ingest/fetch"}],
        # No required Skill dependency: ffmpeg-skill is an optional capability (offload --proxy); ffprobe / ffmpeg are optional binaries.
        "dependencies": [],
        "capabilities": {"optional": [{"id": "ffprobe", "kind": "binary", "used_by": ["ingest/inventory"],
                                       "without_it": "inventory facts are unknown; offload and verify do not need it"},
                                      {"id": "ffmpeg", "kind": "binary", "used_by": ["ingest/inventory"],
                                       "without_it": "LTC is not looked for in the audio of video files (WAV files are still checked with the standard library)"},
                                      {"id": "ffmpeg-skill", "kind": "skill", "install": "npx ffmpeg-skill (or $INGEST_FFMPEG_SKILL_DIR)",
                                       "used_by": ["ingest/offload"],
                                       "without_it": "--proxy is refused (PROXY_UNAVAILABLE, nothing written); everything else works without it"},
                                      {"id": "xxhash", "kind": "python-package", "install": "pip install 'ingest-skill[xxhash]'",
                                       "used_by": ["ingest/offload", "ingest/verify"],
                                       "without_it": "--hash xxh64 is refused (HASH_UNAVAILABLE, nothing written); sha256 + c4 need only the standard library"},
                                      {"id": "yt-dlp", "kind": "python-package", "install": "pip install 'ingest-skill[fetch]'",
                                       "used_by": ["ingest/fetch"],
                                       "without_it": "fetch is refused (FETCH_UNAVAILABLE, nothing written); offload, verify and inventory do not need it"}]},
        "not_provided": ["editing or transcoding (video-editing-skill / ffmpeg-skill)",
                         "encoding proxies itself (offload --proxy queues ffmpeg-skill proxy; the encoder is ffmpeg-skill's)",
                         "cloud upload (publish-skill)", "deleting or formatting the source card",
                         "ASC MHL directory / root hashes, rename, flatten and collections (not yet)",
                         "recording a verify run as an ASC MHL generation (verify writes nothing)",
                         "legacy MHL v1 (pre-ASC) files", "network access outside ingest fetch",
                         "authenticated downloads (cookies, logins, .netrc, browser cookies) and DRM-protected media (never circumvented)",
                         "playlists and channels (fetch takes the URL of one video)",
                         "Panasonic P2, Canon XF and RED card structures (their media are listed file by file, as generic)",
                         "interpreting AVCHD clip-info (.CPI) or Blackmagic RAW .sidecar contents (listed with their clip, not read)",
                         "grouping a recording continued on another card (each card is inventoried on its own)",
                         "telling whether a recorder still holds a growing file open (offload --follow seals on the settle window only)",
                         "live streaming or playout",
                         "decoding LTC (it is detected on a channel, its value comes from the container or the sidecar)",
                         "measuring sync offsets (sync groups are clock-based candidates for ffmpeg-skill multicam / sync)"],
        "tools": TOOLS,
        "execution": {"canonical_invocation": ["ingest", "<command>", "...", "--json"], "network": True, "network_tools": ["ingest/fetch"], "shell": False,
                      "source_is_read_only": True, "overwrites": "never: identical files are skipped, different ones are conflicts"},
        # "manifest" and "inventory" are kept as consumers pinned them; what later steps added has keys of its own below.
        "manifest": {"name": "ingest-manifest.json", "schema": "ingest/manifest@1", "hash": ["sha256", "xxh64"], "default_hash": "sha256"},
        # offload --progress (ADR-009): JSON lines on stderr; stdout stays the one report document.
        "progress": {"stream": "stderr", "format": "JSON lines", "events": ["start", "progress", "file", "slow_destination", "done"],
                     "fields": ["bytes_done", "bytes_total", "files_done", "files_total", "mb_s", "eta_s", "elapsed_s", "file", "file_bytes", "file_size",
                                "destinations[].root", "destinations[].bytes_written", "destinations[].mb_s"],
                     "slow_destination": "a destination writing at under 50% of the destinations' median write speed, judged once each has written 64 MB; also in destinations[].warnings",
                     "destination_speed": "bytes written / time spent writing and syncing them (the drive's own speed, not the shared pace)"},
        # offload --proxy (ADR-010): ffmpeg-skill proxy on each verified video file, linked to its original by hash.
        "proxy": {"report_key": "proxy", "folder": "ingest-proxies", "index": "ingest-proxies/proxies.json", "index_schema": "ingest/proxies@1",
                  "made_by": "ffmpeg-skill scripts/proxy.py", "made_from": "the first verified copy, never the card",
                  "hardware": "ffmpeg-skill --hw when its doctor reports hw.platform_ok (VideoToolbox); otherwise its CPU encoder",
                  "inventory_key": "ingest_proxies", "linked_by": "the original's offload hash (manifest)", "failure": "reported; never fails the offload"},
        # offload --follow (ADR-007): recordings still being written are copied as they grow and sealed once stable.
        "follow": {"report_key": "follow", "manifest_key": "growing", "manifest_optional_keys": ["failed", "growing", "card", "fetch"], "seal_when": "size and mtime unchanged for the settle window",
                   "default_settle_s": 30, "default_poll_s": 2, "copies": "appended bytes only, each appended range read back",
                   "not_detected": "whether the recorder still holds the file open (no portable API)"},
        "asc_mhl": {"spec": "ASC MHL v1.0 (XML version 2.0)", "folder": "ascmhl", "chain": "ascmhl_chain.xml", "process": "transfer",
                    "recorded_format": {"sha256": "c4", "xxh64": "xxh64"}, "verified_formats": ["c4", "md5", "sha1", "xxh64", "xxh3", "xxh128"]},
        "inventory": {"schema": "ingest/inventory@2", "structures": ["sony_xdcam", "sony_xdcam_ex", "avchd", "blackmagic_raw", "generic"],
                      "reel_precedence": ["operator", "manifest", "clip_names", "fingerprint"]},
        # Roadmap step 9 (ADR-008): per inventory clip "sync"; several folders give ingest/inventory-set@1 with sync_groups.
        "sync": {"clip_key": "sync", "set_schema": "ingest/inventory-set@1", "facts": ["start_timecode", "start_tod_s", "frame_rate", "timecode_rate", "ltc", "device", "wall_clock_start", "duration_s"],
                               "sources": ["ffprobe", "sony_nonrealtimemeta", "bwf_bext", "ixml", "wav_data_chunk", "container_creation_time"],
                               "ltc": "detected (biphase-mark signal), never decoded; WAV with the standard library, other files with ffmpeg when on PATH",
                 "group_bases": ["timecode", "utc", "local"], "group_for": "ffmpeg-skill multicam / sync (scripts/multicam.py)"},
        "fetch": {"schema": "ingest/fetch@1", "downloader": "yt-dlp (library, optional extra)", "manifest_key": "fetch",
                  "recorded": ["source_url", "webpage_url", "redacted", "extractor", "extractor_key", "downloader", "format_selector", "format_id",
                               "id", "title", "duration", "is_live", "was_live", "live_from_start", "files", "started_at", "fetched_at"],
                  "never_recorded": ["cookies", "request headers", "credentials in URLs (userinfo, token / signature / key / password-like parameters)", "the media URL downloaded from"]},
        "errors": ERRORS,
    }
