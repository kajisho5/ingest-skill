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
from .core import NAME_BY, IngestError, doctor, inventory, inventory_set, offload, verify
from .fetch import fetch
from .hashing import DEFAULT_HASH, OFFLOAD_HASHES
from .progress import json_lines
from . import rename as rename_mod


def _emit(doc: Dict[str, Any], as_json: bool, summary: str) -> None:
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(summary)


def _offload_summary(d: Dict[str, Any]) -> str:
    reel = d["card"]["reel"]
    lines = [f"{'DRY RUN: ' if d['dry_run'] else ''}{len(d['files'])} files, {d['bytes_total'] / 1e9:.2f} GB from {d['source']} ({d['hash']}), reel {reel['id']} ({reel['source']})"]
    for dst in d["destinations"]:
        lines.append(f"  {dst['root']}: copied {dst['copied']}, identical {dst['skipped_identical']}, conflicts {dst['conflicts']}, failed {dst['failed']}"
                     + (f", growing {dst['growing']} (partial copies, not sealed)" if dst.get("growing") else "")
                     + (f", wrote at {dst['write_mb_s']} MB/s" if dst.get("write_mb_s") else ""))
        for w in dst.get("warnings", []):
            lines.append(f"    warning: {w}")
        if dst["mhl"]:
            lines.append(f"    ASC MHL: {dst['mhl']}")
        for prob in dst["problems"]:
            lines.append(f"    {prob['code']}: {prob['detail']}")
        if dst["mhl_failed"]:
            lines.append(f"    {dst['mhl_failed']} file(s) differ from their earlier ASC MHL record")
    if d.get("proxy"):
        px = d["proxy"]
        lines.append(f"  proxies ({px['encoder']}): made {px['made']}, already there {px['exists']}, failed {px['failed']}, skipped {px['skipped']}")
    if d.get("follow"):
        fo = d["follow"]
        lines.append(f"  followed {len(fo['files'])} growing file(s) over {fo['polls']} poll(s), settle {fo['settle_s']:g} s; stopped: {fo['stopped']}")
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
    p.add_argument("--follow", action="store_true",
                   help="copy recordings still being written as they grow (appended bytes only) and seal each one once it has "
                        "stopped changing for --settle seconds; until then it is a verified partial copy listed as growing")
    p.add_argument("--settle", type=float, help="with --follow: seconds a file's size and mtime must stay unchanged before it is sealed (default 30)")
    p.add_argument("--poll", type=float, help="with --follow: seconds between looks at the source (default 2)")
    p.add_argument("--follow-for", type=float, help="with --follow: stop after this many seconds, leaving unsealed files as growing partial copies")
    p.add_argument("--proxy", action="store_true",
                   help="queue ffmpeg-skill proxy for each verified video file (with --hw when its doctor reports it), into "
                        "<destination>/ingest-proxies/, while the next files copy; a failed proxy does not fail the offload")
    p.add_argument("--ffmpeg-skill", help="with --proxy: the ffmpeg-skill folder (default $INGEST_FFMPEG_SKILL_DIR or ~/.claude/skills/ffmpeg-skill)")
    p.add_argument("--proxy-width", type=int, help="with --proxy: proxy width in pixels (default: ffmpeg-skill's, 640)")
    p.add_argument("--progress", action="store_true",
                   help="write progress as JSON lines to stderr: bytes done, MB/s overall and per destination, ETA, and a warning "
                        "when one destination writes much slower than the others")
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
    p = sub.add_parser("inventory", help="clip list of a card or an offloaded folder: one entry per recording, sidecars, reel, sync facts; "
                                         "several folders: also the sync-group candidates across them (multicam)")
    p.add_argument("target", nargs="+")
    p.add_argument("--reel", help="the card's reel / card id, overriding the manifest, the clip names and the fingerprint")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("rename", help="rename the clips of an offloaded copy from a template: plan (preview, writes nothing), apply (the previewed "
                                      "plan), undo (EXPERIMENTAL, ADR-015)")
    rsub = p.add_subparsers(dest="rename_cmd", required=True)
    for name, hlp in (("plan", "old -> new name of every clip, each field's value and reason; writes nothing"),
                      ("apply", "run the plan a preview printed (--plan-id): rename (or --copy-to), manifest, ASC MHL, undo manifest"),
                      ("undo", "rename the files of the last applied rename (or --undo FILE) back to their original names")):
        q = rsub.add_parser(name, help=hlp)
        q.add_argument("folder")
        q.add_argument("--json", action="store_true")
        if name == "undo":
            q.add_argument("--undo", dest="undo_file", help="an ingest-rename/<time>.json undo manifest (default: the last applied one)")
            continue
        q.add_argument("--template", default=rename_mod.DEFAULT_TEMPLATE,
                       help="file name template ending with {ext}; fields {name} or {name:spec} (spec: [prefix][0N] or a strftime format for "
                            "date / time); a missing field is dropped with its separator (default %(default)s)")
        q.add_argument("--set", dest="sets", action="append", default=[], metavar="FIELD=VALUE", help="a value for every clip, e.g. event=ConfName, room=Room A, camera=CamB")
        q.add_argument("--fields", help="per-clip values (ingest/rename-fields@1), e.g. scene and speaker from your own analysis")
        q.add_argument("--utc-offset", help="move UTC capture times (container creation time) to local time, e.g. +09:00")
        q.add_argument("--date-from-mtime", action="store_true", help="when a clip has no recorded capture time, use its file modification time")
        q.add_argument("--copy-to", help="copy each clip and its sidecars into this folder under the new names (verified), instead of renaming in place")
        if name == "apply":
            q.add_argument("--plan-id", required=True, help="the plan_id that `ingest rename plan` printed for the same folder and options")
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
            d = offload(a.source, a.dest, project=a.project, dry_run=a.dry_run, hash_algorithm=a.hash, name_by=a.name_by, reel=a.reel,
                        follow=a.follow, settle_s=a.settle, poll_s=a.poll, follow_for_s=a.follow_for,
                        progress=json_lines(sys.stderr) if a.progress else None,
                        proxy=a.proxy, ffmpeg_skill=a.ffmpeg_skill, proxy_width=a.proxy_width).to_dict()
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
        if a.cmd == "rename":
            if a.rename_cmd == "undo":
                d = rename_mod.undo(a.folder, a.undo_file)
            else:
                opts = dict(template=a.template, values=rename_mod.parse_sets(a.sets), fields_file=a.fields, utc_offset=a.utc_offset,
                            date_from_mtime=a.date_from_mtime, copy_to=a.copy_to)
                d = rename_mod.plan(a.folder, **opts) if a.rename_cmd == "plan" else rename_mod.apply(a.folder, a.plan_id, **opts)
            if a.rename_cmd == "plan":
                lines = [f"{d['mode']}: {d['counts']['rename'] + d['counts']['copy']} to {'copy' if d['mode'] == 'copy' else 'rename'}, "
                         f"{d['counts']['unchanged']} unchanged, {d['counts']['skipped']} skipped; plan_id {d['plan_id']} (nothing written)"]
                for r in d["rows"]:
                    lines.append(f"  {r['from']} -> {r['to'] if r['files'] else '(kept)'}" + (f"  [{r['reason']}]" if r["reason"] else ""))
                    for f in r["files"][1:]:
                        lines.append(f"      {f['from']} -> {f['to']} ({f['role']})")
                lines += [f"  warning: {w}" for w in d["warnings"]]
                _emit(d, a.json, "\n".join(lines))
                return 0
            _emit(d, a.json, f"{d['action']}: {d['moved']} file(s) " + ("done" if d["ok"] else "NOT done: " + "; ".join(
                f"{x.get('code')}: {x.get('path') or x.get('detail')}" for x in d["failures"]))
                  + (f"; undo manifest {d['undo']}" if d.get("undo") else ""))
            return 0 if d["ok"] else ERRORS["INCOMPLETE"]["exit_code"]
        if a.cmd == "inventory" and len(a.target) > 1:
            if a.reel:
                raise IngestError("REEL_INVALID", "--reel names one card: give one folder with it")
            d = inventory_set(a.target)
            lines = [f"{d['card_count']} cards, {d['clip_count']} clips; {len(d['sync_groups'])} sync group candidate(s)"]
            for g in d["sync_groups"]:
                lines.append(f"  {g['id']} ({g['basis']}): " + ", ".join(f"{m['card']}/{m['clip_id']}{' (reference)' if m['reference'] else ''}" for m in g["members"]))
            _emit(d, a.json, "\n".join(lines))
            return 0
        if a.cmd == "inventory":
            d = inventory(a.target[0], reel=a.reel)
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
