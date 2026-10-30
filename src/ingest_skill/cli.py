"""`ingest` command line. Every command prints one JSON document with --json (a short human summary otherwise).

Exit codes: 0 ok, 2 refused (bad input, nothing written), 3 incomplete (a conflict, failure, missing or changed file).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .contract import ERRORS, contract
from .core import NAME_BY, IngestError, doctor, inventory, offload, verify
from .fetch import fetch
from .hashing import DEFAULT_HASH, OFFLOAD_HASHES


def _emit(doc: Dict[str, Any], as_json: bool, summary: str) -> None:
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(summary)


def _offload_summary(d: Dict[str, Any]) -> str:
    reel = d["card"]["reel"]
    lines = [f"{'DRY RUN: ' if d['dry_run'] else ''}{len(d['files'])} files, {d['bytes_total'] / 1e9:.2f} GB from {d['source']} ({d['hash']}), reel {reel['id']} ({reel['source']})"]
    for dst in d["destinations"]:
        lines.append(f"  {dst['root']}: copied {dst['copied']}, identical {dst['skipped_identical']}, conflicts {dst['conflicts']}, failed {dst['failed']}")
        if dst["mhl"]:
            lines.append(f"    ASC MHL: {dst['mhl']}")
        for prob in dst["problems"]:
            lines.append(f"    {prob['code']}: {prob['detail']}")
        if dst["mhl_failed"]:
            lines.append(f"    {dst['mhl_failed']} file(s) differ from their earlier ASC MHL record")
    if d["skipped"]:
        lines.append(f"  skipped {len(d['skipped'])} (card housekeeping, symbolic links, the folder's own manifest / ascmhl history)")
    lines.append("OK: every copy verified" if d["ok"] else "INCOMPLETE: see conflicts / failures (run with --json for details)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ingest", description="Verified media offload, dual backup and clip inventory.")
    ap.add_argument("--version", action="version", version=f"ingest-skill {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("contract", "doctor"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("offload", help="copy a card / recorder folder to one or more destinations, verified")
    p.add_argument("source")
    p.add_argument("--dest", action="append", default=[], required=True, help="destination root; repeat for a second copy on another drive")
    p.add_argument("--project", help="sub-folder under each destination (e.g. the event name)")
    p.add_argument("--dry-run", action="store_true", help="list what would be copied, write nothing")
    p.add_argument("--hash", choices=OFFLOAD_HASHES, default=DEFAULT_HASH,
                   help="checksum for the read-back and ingest-manifest.json (default sha256; xxh64 needs the xxhash extra). "
                        "The ASC MHL records c4 with sha256, xxh64 with xxh64")
    p.add_argument("--name-by", choices=NAME_BY, default="folder",
                   help="name of the card folder in each destination: the source folder's name (default) or the card's reel id "
                        "(--reel, else the camera ID + reel number of its clip names, else CARD-<fingerprint>)")
    p.add_argument("--reel", help="the card's reel / card id, overriding what the card says (letters, digits, . _ -)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("fetch", help="download one video URL with yt-dlp (optional extra), then offload it like a card, verified")
    p.add_argument("url", help="the URL of one video; you must have the right to download it")
    p.add_argument("--dest", action="append", default=[], required=True, help="destination root; repeat for a second copy on another drive")
    p.add_argument("--project", help="sub-folder under each destination (e.g. the event name)")
    p.add_argument("--name", help="the folder of this download in each destination (default <extractor>-<video id>)")
    p.add_argument("--hash", choices=OFFLOAD_HASHES, default=DEFAULT_HASH, help="checksum for the read-back and ingest-manifest.json, as for offload")
    p.add_argument("--format", dest="format_selector", help="yt-dlp format selector (default bestvideo*+bestaudio/best, or best without ffmpeg)")
    p.add_argument("--staging", help="folder to download into before the verified copy (default: the system temporary folder)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="re-hash an offloaded folder against its manifest (sha256 or xxh64) and its ASC MHL history")
    p.add_argument("target")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("inventory", help="clip list of a card or an offloaded folder: one entry per recording, sidecars, reel")
    p.add_argument("target")
    p.add_argument("--reel", help="the card's reel / card id, overriding the manifest, the clip names and the fingerprint")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "contract":
            _emit(contract(), True, "")
            return 0
        if a.cmd == "doctor":
            d = doctor()
            _emit(d, a.json, "\n".join(f"{k}: {v['status']} {v.get('detail', '')}" for k, v in d["checks"].items()))
            return 0 if d["ok"] else 3
        if a.cmd == "offload":
            d = offload(a.source, a.dest, project=a.project, dry_run=a.dry_run, hash_algorithm=a.hash, name_by=a.name_by, reel=a.reel).to_dict()
            _emit(d, a.json, _offload_summary(d))
            return 0 if d["ok"] else ERRORS["INCOMPLETE"]["exit_code"]
        if a.cmd == "fetch":
            d = fetch(a.url, a.dest, project=a.project, name=a.name, hash_algorithm=a.hash, format_selector=a.format_selector, staging=a.staging)
            f = d["fetch"]
            summary = (f"fetched {f['source_url']} ({f['extractor']}, format {f['format_id']}, yt-dlp {f['downloader']['version']})\n"
                       + _offload_summary(d) + f"\n  staging {d['staging']['path']}: {d['staging']['detail']}"
                       + "".join(f"\n  warning: {w}" for w in d["warnings"]))
            _emit(d, a.json, summary)
            return 0 if d["ok"] else ERRORS["INCOMPLETE"]["exit_code"]
        if a.cmd == "verify":
            d = verify(a.target)
            mhl_note = "" if d["mhl"] is None else f"; ASC MHL {d['mhl']['generations']} generation(s), {len(d['mhl']['problems'])} problem(s)"
            _emit(d, a.json, f"{d['ok_files']}/{d['checked']} verified; missing {len(d['missing'])}, changed {len(d['changed'])}, unlisted {len(d['unlisted'])}{mhl_note}")
            return 0 if d["ok"] else ERRORS["INCOMPLETE"]["exit_code"]
        if a.cmd == "inventory":
            d = inventory(a.target, reel=a.reel)
            reel = d["card"]["reel"]
            structures = ", ".join(s["id"] for s in d["card"]["structures"])
            _emit(d, a.json, f"reel {reel['id']} ({reel['source']}); {structures}; {d['clip_count']} clips from {d['file_count']} files "
                             f"({d['spanned_count']} spanned), {d['total_duration_s']:.1f} s ({d['probe']})"
                             + "".join(f"\n  warning: {w}" for w in d["warnings"]))
            return 0
    except IngestError as e:
        doc = {"ok": False, "error": {"code": e.code, "message": str(e)}}
        print(json.dumps(doc, ensure_ascii=False) if getattr(a, "json", False) else f"error {e.code}: {e}", file=sys.stdout if getattr(a, "json", False) else sys.stderr)
        return ERRORS.get(e.code, {"exit_code": 2})["exit_code"]
    return 2


if __name__ == "__main__":
    sys.exit(main())
