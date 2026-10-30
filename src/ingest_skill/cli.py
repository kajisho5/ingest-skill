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
from .core import IngestError, doctor, inventory, offload, verify


def _emit(doc: Dict[str, Any], as_json: bool, summary: str) -> None:
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(summary)


def _offload_summary(d: Dict[str, Any]) -> str:
    lines = [f"{'DRY RUN: ' if d['dry_run'] else ''}{len(d['files'])} files, {d['bytes_total'] / 1e9:.2f} GB from {d['source']}"]
    for dst in d["destinations"]:
        lines.append(f"  {dst['root']}: copied {dst['copied']}, identical {dst['skipped_identical']}, conflicts {dst['conflicts']}, failed {dst['failed']}")
    if d["skipped"]:
        lines.append(f"  skipped {len(d['skipped'])} (card housekeeping / symbolic links)")
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
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="re-hash an offloaded folder against its manifest")
    p.add_argument("target")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("inventory", help="clip list of a folder")
    p.add_argument("target")
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
            d = offload(a.source, a.dest, project=a.project, dry_run=a.dry_run).to_dict()
            _emit(d, a.json, _offload_summary(d))
            return 0 if d["ok"] else ERRORS["INCOMPLETE"]["exit_code"]
        if a.cmd == "verify":
            d = verify(a.target)
            _emit(d, a.json, f"{d['ok_files']}/{d['checked']} verified; missing {len(d['missing'])}, changed {len(d['changed'])}, unlisted {len(d['unlisted'])}")
            return 0 if d["ok"] else ERRORS["INCOMPLETE"]["exit_code"]
        if a.cmd == "inventory":
            d = inventory(a.target)
            _emit(d, a.json, f"{d['clip_count']} clips, {d['total_duration_s']:.1f} s ({d['probe']})")
            return 0
    except IngestError as e:
        doc = {"ok": False, "error": {"code": e.code, "message": str(e)}}
        print(json.dumps(doc, ensure_ascii=False) if getattr(a, "json", False) else f"error {e.code}: {e}", file=sys.stdout if getattr(a, "json", False) else sys.stderr)
        return ERRORS.get(e.code, {"exit_code": 2})["exit_code"]
    return 2


if __name__ == "__main__":
    sys.exit(main())
