"""Machine-readable contract (`ingest contract --json`), in the shape the AI Video Production OS registry reads
(skill_id, version, contract_version, provides, dependencies, not_provided)."""
from __future__ import annotations

from typing import Any, Dict

from . import __version__

SKILL_ID = "ingest"
CONTRACT_VERSION = "1.0"

TOOLS = [
    {"tool_id": "ingest/offload", "role": "execution", "writes_media": True, "mutates_input": False, "deterministic": True,
     "description": "Copy every file of a card or recorder folder to one or more destinations, verified by a sha256 (default) or xxh64 read-back; writes ingest-manifest.json and an ASC MHL history generation (ascmhl/) in each",
     "argv": ["offload", "<source>", "--dest", "<dir>", "[--dest <dir> ...]", "[--project <name>]", "[--hash sha256|xxh64]", "[--name-by folder|reel]", "[--reel <id>]", "[--dry-run]", "--json"]},
    {"tool_id": "ingest/verify", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "Re-hash an offloaded folder against its manifest (sha256 or xxh64) and its ASC MHL history: missing, changed and unlisted files; writes nothing",
     "argv": ["verify", "<folder-or-manifest>", "--json"]},
    {"tool_id": "ingest/inventory", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "Clip list of a card or an offloaded folder, one entry per recording: card structure (Sony XDCAM / XAVC, XDCAM EX, AVCHD, Blackmagic RAW, generic), spanned files grouped in order, camera sidecar metadata, reel / card id, manifest hash and ffprobe facts",
     "argv": ["inventory", "<folder>", "[--reel <id>]", "--json"]},
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
                     {"id": "ingest.inventory", "lifecycle": "EXPERIMENTAL", "tool_id": "ingest/inventory"}],
        # No Skill dependencies (ffmpeg-skill is not used); ffprobe is an optional binary, not a Skill.
        "dependencies": [],
        "capabilities": {"optional": [{"id": "ffprobe", "kind": "binary", "used_by": ["ingest/inventory"],
                                       "without_it": "inventory facts are unknown; offload and verify do not need it"},
                                      {"id": "xxhash", "kind": "python-package", "install": "pip install 'ingest-skill[xxhash]'",
                                       "used_by": ["ingest/offload", "ingest/verify"],
                                       "without_it": "--hash xxh64 is refused (HASH_UNAVAILABLE, nothing written); sha256 + c4 need only the standard library"}]},
        "not_provided": ["editing or transcoding (video-editing-skill / ffmpeg-skill)", "proxy generation (ffmpeg-skill proxy)",
                         "cloud upload (publish-skill)", "deleting or formatting the source card",
                         "ASC MHL directory / root hashes, rename, flatten and collections (not yet)",
                         "recording a verify run as an ASC MHL generation (verify writes nothing)",
                         "legacy MHL v1 (pre-ASC) files", "network access of any kind",
                         "Panasonic P2, Canon XF and RED card structures (their media are listed file by file, as generic)",
                         "interpreting AVCHD clip-info (.CPI) or Blackmagic RAW .sidecar contents (listed with their clip, not read)",
                         "grouping a recording continued on another card (each card is inventoried on its own)"],
        "tools": TOOLS,
        "execution": {"canonical_invocation": ["ingest", "<command>", "...", "--json"], "network": False, "shell": False,
                      "source_is_read_only": True, "overwrites": "never: identical files are skipped, different ones are conflicts"},
        "manifest": {"name": "ingest-manifest.json", "schema": "ingest/manifest@1", "hash": ["sha256", "xxh64"], "default_hash": "sha256"},
        "asc_mhl": {"spec": "ASC MHL v1.0 (XML version 2.0)", "folder": "ascmhl", "chain": "ascmhl_chain.xml", "process": "transfer",
                    "recorded_format": {"sha256": "c4", "xxh64": "xxh64"}, "verified_formats": ["c4", "md5", "sha1", "xxh64", "xxh3", "xxh128"]},
        "inventory": {"schema": "ingest/inventory@2", "structures": ["sony_xdcam", "sony_xdcam_ex", "avchd", "blackmagic_raw", "generic"],
                      "reel_precedence": ["operator", "manifest", "clip_names", "fingerprint"]},
        "errors": ERRORS,
    }
