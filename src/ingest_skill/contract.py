"""Machine-readable contract (`ingest contract --json`), in the shape the AI Video Production OS registry reads
(skill_id, version, contract_version, provides, dependencies, not_provided)."""
from __future__ import annotations

from typing import Any, Dict

from . import __version__

SKILL_ID = "ingest"
CONTRACT_VERSION = "1.0"

TOOLS = [
    {"tool_id": "ingest/offload", "role": "execution", "writes_media": True, "mutates_input": False, "deterministic": True,
     "description": "Copy every file of a card or recorder folder to one or more destinations, verified by sha256 read-back; writes ingest-manifest.json in each",
     "argv": ["offload", "<source>", "--dest", "<dir>", "[--dest <dir> ...]", "[--project <name>]", "[--dry-run]", "--json"]},
    {"tool_id": "ingest/verify", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "Re-hash an offloaded folder against its manifest: missing, changed and unlisted files",
     "argv": ["verify", "<folder-or-manifest>", "--json"]},
    {"tool_id": "ingest/inventory", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "Clip list of a folder: size, manifest hash and ffprobe facts (duration, codecs, resolution, timecode)",
     "argv": ["inventory", "<folder>", "--json"]},
]

ERRORS = {
    "SOURCE_NOT_FOUND": {"exit_code": 2, "retryable": False},
    "NO_DESTINATION": {"exit_code": 2, "retryable": False},
    "DUPLICATE_DESTINATION": {"exit_code": 2, "retryable": False},
    "DESTINATION_OVERLAPS_SOURCE": {"exit_code": 2, "retryable": False},
    "MANIFEST_NOT_FOUND": {"exit_code": 2, "retryable": False},
    "MANIFEST_INVALID": {"exit_code": 2, "retryable": False},
    "INCOMPLETE": {"exit_code": 3, "retryable": True},
    # Per-file failures: recorded in that file's `detail`, and the run ends INCOMPLETE.
    "SOURCE_CHANGED": {"exit_code": 3, "retryable": True},
    "VERIFY_MISMATCH": {"exit_code": 3, "retryable": True},
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
                                       "without_it": "inventory facts are unknown; offload and verify do not need it"}]},
        "not_provided": ["editing or transcoding (video-editing-skill / ffmpeg-skill)", "proxy generation (ffmpeg-skill proxy)",
                         "cloud upload (publish-skill)", "deleting or formatting the source card", "MHL / ASC MHL output (not yet)",
                         "xxHash (sha256 only)", "network access of any kind"],
        "tools": TOOLS,
        "execution": {"canonical_invocation": ["ingest", "<command>", "...", "--json"], "network": False, "shell": False,
                      "source_is_read_only": True, "overwrites": "never: identical files are skipped, different ones are conflicts"},
        "manifest": {"name": "ingest-manifest.json", "schema": "ingest/manifest@1", "hash": "sha256"},
        "errors": ERRORS,
    }
