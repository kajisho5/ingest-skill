"""Template renaming of an offloaded copy (ADR-015, roadmap step 13): `ingest rename plan | apply | undo`. EXPERIMENTAL.

    ingest rename plan  <folder> --template T [--set k=v ...] [--fields F] [--copy-to DIR]  → ingest/rename-plan@1 (writes nothing)
    ingest rename apply <folder> (same options) --plan-id ID                                → ingest/rename-report@1
    ingest rename undo  <folder> [--undo FILE]                                               → ingest/rename-report@1

- **Fields.** Each clip of the inventory (one per recording) gets the template's fields: `date` / `time` (the clip's wall-clock
  start, sync.py), `reel` (the card's reel / card id), `camera` (given, else a camera reel), `model` (the device model), `seq` (the
  clip's place in capture order), `orig` (its current name), `ext` (lower case), and every value given with `--set` or, per clip,
  in a fields file (`ingest/rename-fields@1`, e.g. `scene` and `speaker` from the caller's own analysis). Each value carries the
  reason it was chosen. A missing field is dropped with its separator.
- **Safe names.** Unicode NFC; no character any of macOS, Windows or Linux refuses (`<>:"/\\|?*`, control characters); no Windows
  reserved device name (CON, NUL, COM1, ...); no trailing dot or space; at most 255 UTF-8 bytes for the media file and each sidecar.
- **Never overwrites.** A name already taken in the folder (compared without case, NFC) gets a counter (`_2`, `_3`, ...).
- **Sidecars follow.** Files beside a clip whose names start with its basename (XML, XMP, LRF, THM, SRT, ...) keep that basename.
- **In place** only on an offloaded copy (an `ingest-manifest.json`; the card is never modified) whose copy is complete, and only for
  `generic` clips: a structured card's index files name its clips, so those are kept (use `--copy-to`). Every file is re-hashed
  against the manifest before anything is renamed; the manifest is rewritten with the new paths and an ASC MHL generation (process
  `in-place`) records each new path with its `previousPath`, so `verify` and `inventory` pass afterwards. The undo manifest
  (`ingest-rename/<time>.json`) is written before the first rename; `undo` renames everything back the same way.
- **Copy** (`--copy-to DIR`): each clip and its sidecars are copied into DIR under the new names, each copy read back (as offload),
  with DIR's own `ingest-manifest.json` and ASC MHL history. The folder itself is only read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, mhl
from .core import MANIFEST_NAME, MANIFEST_SCHEMA, RENAME_FOLDER, _copy_verified, _iso, _now, inventory, load_manifest
from .errors import IngestError
from .hashing import MHL_FORMAT_FOR, hash_file, require

PLAN_SCHEMA = "ingest/rename-plan@1"
REPORT_SCHEMA = "ingest/rename-report@1"
UNDO_SCHEMA = "ingest/rename-undo@1"
FIELDS_SCHEMA = "ingest/rename-fields@1"
DEFAULT_TEMPLATE = "{date}_{event}_{room}_{camera}_{scene:S02}_{speaker:Speaker-}_{seq:04}{ext}"
SIDECAR_EXTENSIONS = {".xml", ".xmp", ".lrf", ".thm", ".srt", ".json", ".txt", ".cpi", ".sidecar", ".jpg", ".bim", ".ppn"}
# Fields worked out here. seq, orig and ext alone are not metadata: a clip with nothing else keeps its name.
COMPUTED = ("date", "time", "reel", "camera", "model", "seq", "orig", "ext")
NOT_METADATA = {"seq", "orig", "ext"}
FIELD_RE = re.compile(r"\{([a-z][a-z0-9_]*)(?::([^{}]*))?\}")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
FORBIDDEN = set('<>:"/\\|?*')
SEPARATORS = set("_-. ")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}
NAME_BYTES = 255
COUNTER_RESERVE = 4        # room for "_999" so a counter never pushes a name over the limit
MAX_VALUE = 64             # characters of one field value in a name
MAX_FIELDS_FILE = 8 * 1024 * 1024


# ---------------------------------------------------------------- names

def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def safe_value(value: Any) -> str:
    """One field value as part of a file name: NFC, whitespace and characters some OS refuses turned into '-', runs of '-'
    collapsed, separators trimmed from both ends, at most MAX_VALUE characters. "" when nothing is left (the field is missing)."""
    s = nfc(str(value))
    out = []
    for ch in s:
        if ch in FORBIDDEN or ord(ch) < 32 or ord(ch) == 127 or ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            out.append("-")
        else:
            out.append(ch)
    s = re.sub(r"-{2,}", "-", "".join(out)).strip("".join(SEPARATORS))
    return s[:MAX_VALUE].rstrip("".join(SEPARATORS))


def _fit(stem: str, budget: int) -> str:
    """stem cut (by characters, never inside one) to at most budget UTF-8 bytes."""
    while len(stem.encode("utf-8")) > budget:
        stem = stem[:-1]
    return stem.rstrip("".join(SEPARATORS))


def safe_stem(stem: str, longest_suffix: int) -> str:
    """A whole stem made safe on macOS, Windows and Linux. longest_suffix: the UTF-8 bytes of the longest thing appended to it
    (the extension, or a sidecar's remainder and extension)."""
    stem = nfc(stem).strip("".join(SEPARATORS))
    stem = _fit(stem, NAME_BYTES - longest_suffix - COUNTER_RESERVE)
    if stem.split(".")[0].upper() in WINDOWS_RESERVED:
        head, dot, tail = stem.partition(".")
        stem = head + "_" + dot + tail
    return stem.rstrip(". ")


def parse_template(template: str) -> List[Tuple[str, str, str]]:
    """[(kind, text, spec)]: ("lit", text, "") and ("field", name, spec). The template must end with {ext}; its literal text must
    be safe in a name."""
    if not isinstance(template, str) or not template:
        raise IngestError("TEMPLATE_INVALID", "the template is empty")
    parts: List[Tuple[str, str, str]] = []
    pos = 0
    for m in FIELD_RE.finditer(template):
        if m.start() > pos:
            parts.append(("lit", template[pos:m.start()], ""))
        parts.append(("field", m.group(1), m.group(2) or ""))
        pos = m.end()
    if pos < len(template):
        parts.append(("lit", template[pos:], ""))
    for kind, text, spec in parts:
        if kind == "lit" and (set(text) & (FORBIDDEN | {"{", "}"}) or any(ord(c) < 32 for c in text)):
            raise IngestError("TEMPLATE_INVALID", f"template text {text!r}: braces must hold a field ({{name}} or {{name:spec}}) and "
                                                  f"the text may not contain {''.join(sorted(FORBIDDEN))} or control characters")
        if kind == "field" and (set(spec) & FORBIDDEN or any(ord(c) < 32 for c in spec)) and "%" not in spec:
            raise IngestError("TEMPLATE_INVALID", f"{{{text}:{spec}}}: the spec may not contain {''.join(sorted(FORBIDDEN))}")
    if not parts or parts[-1] != ("field", "ext", ""):
        raise IngestError("TEMPLATE_INVALID", "the template must end with {ext} (the clip's own extension)")
    if [p for p in parts if p[0] == "field"].count(("field", "ext", "")) != 1 or any(p[1] == "ext" and p[2] for p in parts if p[0] == "field"):
        raise IngestError("TEMPLATE_INVALID", "{ext} appears once, at the end, without a spec")
    if not any(p[0] == "field" and p[1] != "ext" for p in parts):
        raise IngestError("TEMPLATE_INVALID", "the template names no field besides {ext}")
    return parts


def render_field(name: str, spec: str, value: Any) -> str:
    """One field as text: date / time with a strftime spec (default %Y-%m-%d / %H%M%S); otherwise spec = [prefix][0N]: the prefix
    is put before the value, 0N pads a whole number to N digits. "" when the value is missing."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        fmt = spec if "%" in spec else ("%Y-%m-%d" if name == "date" else "%H%M%S" if name == "time" else "%Y-%m-%dT%H%M%S")
        return safe_value(value.strftime(fmt))
    m = re.fullmatch(r"(.*?)(0\d+)?", spec)
    prefix, pad = (m.group(1), m.group(2)) if m else (spec, None)
    text = str(value)
    if pad and re.fullmatch(r"\d+", text):
        text = text.zfill(int(pad))
    v = safe_value(text)
    return safe_value(prefix + v) if v else ""


def compose(parts: Sequence[Tuple[str, str, str]], rendered: Dict[str, str]) -> str:
    """The stem from the template. A literal made only of separators (_ - . space) joins two fields that both have a value: a
    missing field is dropped with its separator, so no separator is doubled, leads or trails. Other literal text is kept."""
    out: List[str] = []
    pending: Optional[str] = None
    for kind, text, _ in parts:
        if kind == "field":
            if text == "ext":
                continue
            v = rendered.get(text, "")
            if not v:
                continue
            if pending is not None and out:
                out.append(pending)
            pending = None
            out.append(v)
        elif text and set(text) <= SEPARATORS:
            if pending is None:
                pending = text
        else:
            if pending is not None and out:
                out.append(pending)
            pending = None
            out.append(text)
    return re.sub(r"([_\-. ])\1+", r"\1", "".join(out))


# ---------------------------------------------------------------- fields

def _parse_dt(text: str) -> Optional[datetime]:
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        try:
            return datetime.fromisoformat(t[:19])
        except ValueError:
            return None


def parse_offset(text: Optional[str]) -> Optional[timezone]:
    if text is None:
        return None
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text.strip())
    if not m or int(m.group(2)) > 23 or int(m.group(3)) > 59:
        raise IngestError("FIELDS_INVALID", f"--utc-offset {text!r}: use +HH:MM or -HH:MM (e.g. +09:00)")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(delta if m.group(1) == "+" else -delta)


def capture_time(clip: Dict, root: Path, offset: Optional[timezone], from_mtime: bool) -> Tuple[Optional[datetime], str]:
    """The clip's capture time as it should read in a name, and why: the camera's own local clock, else a UTC time moved by
    --utc-offset (or kept in UTC, said so), else (only when asked) the file's modification time."""
    wc = (clip.get("sync") or {}).get("wall_clock_start") or {}
    src = wc.get("source") or "metadata"
    if wc.get("local"):
        dt = _parse_dt(wc["local"])
        if dt is not None:
            return dt.replace(tzinfo=None), f"local capture time from {src}"
    if wc.get("utc"):
        dt = _parse_dt(wc["utc"])
        if dt is not None:
            if offset is not None:
                return dt.astimezone(offset).replace(tzinfo=None), f"{src} (UTC) moved by --utc-offset"
            return dt.astimezone(timezone.utc).replace(tzinfo=None), f"{src} in UTC (no local time recorded; --utc-offset gives local time)"
    if from_mtime:
        try:
            st = (root / clip["path"]).stat()
        except OSError:
            return None, ""
        return datetime.fromtimestamp(st.st_mtime), "file modification time (--date-from-mtime; no capture time recorded)"
    return None, ""


def load_fields(path: Optional[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """A fields file: {"schema": "ingest/rename-fields@1", "clips": {<clip path | clip id | hash>: {<field>: value | {value, source}}}}."""
    if not path:
        return {}
    p = Path(path)
    try:
        if p.stat().st_size > MAX_FIELDS_FILE:
            raise IngestError("FIELDS_INVALID", f"{path}: larger than {MAX_FIELDS_FILE} bytes")
        doc = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise IngestError("FIELDS_INVALID", f"{path}: {e}") from None
    except ValueError as e:
        raise IngestError("FIELDS_INVALID", f"{path} is not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("schema") != FIELDS_SCHEMA or not isinstance(doc.get("clips"), dict):
        raise IngestError("FIELDS_INVALID", f"{path} is not an {FIELDS_SCHEMA} document ({{\"schema\", \"clips\": {{...}}}})")
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for key, fields in doc["clips"].items():
        if not isinstance(fields, dict):
            raise IngestError("FIELDS_INVALID", f"{path}: clips[{key!r}] must be an object of fields")
        clean: Dict[str, Dict[str, Any]] = {}
        for name, v in fields.items():
            if not KEY_RE.match(str(name)) or name in NOT_METADATA:
                raise IngestError("FIELDS_INVALID", f"{path}: clips[{key!r}]: {name!r} is not a field that can be given (a-z, 0-9, _; not seq, orig or ext)")
            if isinstance(v, dict):
                if "value" not in v:
                    raise IngestError("FIELDS_INVALID", f"{path}: clips[{key!r}].{name} is an object without \"value\"")
                value, source = v["value"], str(v.get("source") or "fields file")
            else:
                value, source = v, "fields file"
            if value is not None and not isinstance(value, (str, int, float)):
                raise IngestError("FIELDS_INVALID", f"{path}: clips[{key!r}].{name} must be a string or a number")
            clean[name] = {"value": value, "source": source}
        out[str(key)] = clean
    return out


def parse_sets(values: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in values or ():
        key, eq, value = str(item).partition("=")
        key = key.strip()
        if not eq or not KEY_RE.match(key) or key in NOT_METADATA:
            raise IngestError("FIELDS_INVALID", f"--set {item!r}: use name=value with a lower-case name (a-z, 0-9, _; not seq, orig or ext)")
        out[key] = value
    return out


# ---------------------------------------------------------------- plan

def _files_of(clip: Dict, all_rels: Sequence[str]) -> List[Tuple[str, str, str]]:
    """[(path, role, remainder)] of one clip: its members (media) and its sidecars, related files and same-basename sidecar files
    in the folder of its first member. remainder: what follows the media basename in the sidecar's name, extension excluded."""
    members = [m["path"] for m in clip["members"]]
    first = PurePosixPath(members[0])
    stem = first.stem
    out: List[Tuple[str, str, str]] = [(m, "media", "") for m in members]
    seen = set(members)
    candidates = [s["path"] for s in clip.get("sidecars") or []] + list(clip.get("related") or [])
    parent = str(first.parent)
    for rel in all_rels:
        p = PurePosixPath(rel)
        if str(p.parent) == parent and p.suffix.lower() in SIDECAR_EXTENSIONS and p.stem.lower().startswith(stem.lower()) and rel not in members:
            candidates.append(rel)
    for rel in candidates:
        if rel in seen:
            continue
        p = PurePosixPath(rel)
        if str(p.parent) != parent or not p.stem.lower().startswith(stem.lower()):
            continue   # a sidecar that does not carry the clip's basename (a card index, a proxy folder) is not renamed with it
        seen.add(rel)
        out.append((rel, "sidecar", p.stem[len(stem):]))
    return out


def _key(name: str) -> str:
    return nfc(name).casefold()


def _taken_in(folder: Path) -> set:
    try:
        return {_key(n) for n in os.listdir(folder)}
    except OSError:
        return set()


def _resolve(root: Path, copy_to: Optional[str]) -> Optional[Path]:
    if copy_to is None:
        return None
    dest = Path(copy_to).resolve()
    if dest == root or str(dest).startswith(str(root) + os.sep) or str(root).startswith(str(dest) + os.sep):
        raise IngestError("DESTINATION_OVERLAPS_SOURCE", f"--copy-to {copy_to} overlaps the folder {root}: give a folder outside it")
    if dest.exists() and not dest.is_dir():
        raise IngestError("NO_DESTINATION", f"--copy-to {copy_to} is not a directory")
    return dest


def plan(folder: str, template: str = DEFAULT_TEMPLATE, values: Optional[Dict[str, str]] = None, fields_file: Optional[str] = None,
         utc_offset: Optional[str] = None, date_from_mtime: bool = False, copy_to: Optional[str] = None) -> Dict:
    """What `apply` would do, row by row (old name → new name, each field's value and reason). Writes nothing."""
    root = Path(folder).resolve()
    if not root.is_dir():
        raise IngestError("SOURCE_NOT_FOUND", f"{folder} is not a directory")
    parts = parse_template(template)
    given = dict(values or {})
    per_clip = load_fields(fields_file)
    offset = parse_offset(utc_offset)
    dest = _resolve(root, copy_to)
    mode = "copy" if dest is not None else "in_place"
    manifest: Optional[Dict] = None
    if (root / MANIFEST_NAME).is_file():
        _, manifest = load_manifest(str(root))
    refusal = None
    if mode == "in_place":
        if manifest is None:
            raise IngestError("RENAME_REFUSED", f"{folder} has no {MANIFEST_NAME}: rename in place works on an offloaded copy only (the card is never "
                                                "modified); give --copy-to for renamed copies of any folder")
        if manifest.get("failed") or manifest.get("growing"):
            refusal = ("the copy is incomplete (files the offload failed to copy, or recordings still being written): complete it with "
                       "offload first; nothing is renamed")
    inv = inventory(str(root))
    reel = inv["card"]["reel"]
    all_rels = sorted({m["path"] for c in inv["clips"] for m in c["members"]} | {s["path"] for c in inv["clips"] for s in c.get("sidecars") or []}
                      | {r for c in inv["clips"] for r in c.get("related") or []} | set(_folder_files(root)))
    hashes = {e["path"]: e[manifest["hash"]] for e in manifest["files"]} if manifest else {}
    # capture order: clips with a time first, by that time, then the rest by path
    timed = []
    for c in inv["clips"]:
        dt, why = capture_time(c, root, offset, date_from_mtime)
        timed.append((c, dt, why))
    timed.sort(key=lambda t: (0, t[1], t[0]["path"]) if t[1] is not None else (1, datetime.min, t[0]["path"]))
    used_fields = {text for kind, text, _ in parts if kind == "field"}
    taken: Dict[str, set] = {}
    rows = []
    for seq, (c, dt, why) in enumerate(timed, start=1):
        files = _files_of(c, all_rels)
        first = PurePosixPath(files[0][0])
        fields: Dict[str, Dict[str, Any]] = {}

        def put(name: str, value: Any, source: str) -> None:
            if value not in (None, "") and name not in fields:
                fields[name] = {"value": value, "source": source}
        mine = {}
        for key in (c["path"], c["clip_id"], hashes.get(c["path"]), f"sha256:{hashes.get(c['path'])}" if hashes.get(c["path"]) else None):
            if key and key in per_clip:
                mine = per_clip[key]
                break
        for name, v in mine.items():
            put(name, v["value"], v["source"])
        for name, v in given.items():
            put(name, v, f"operator (--set {name})")
        if dt is not None:
            put("date", dt, why)
            put("time", dt, why)
        if reel.get("source") in ("operator", "manifest", "clip_names") and not (reel.get("source") == "manifest" and reel.get("recorded_source") == "fingerprint"):
            put("camera", reel["id"], f"card reel ({reel['source']})")
            put("reel", reel["id"], f"card reel ({reel['source']})")
        else:
            put("reel", reel["id"], "card fingerprint (not a camera reel)")
        model = ((c.get("sync") or {}).get("device") or {}).get("model")
        put("model", model, "device model (camera metadata)")
        put("seq", seq, "place in capture order" if dt is not None else "place in capture order (no capture time: after the timed clips, by name)")
        put("orig", first.stem, "the current name")
        fields["ext"] = {"value": first.suffix.lower(), "source": "the clip's extension, lower case"}
        rendered = {}
        for kind, name, spec in parts:
            if kind == "field" and name != "ext":
                if name in fields:
                    if isinstance(fields[name]["value"], str) and name in ("date", "time"):
                        d = _parse_dt(fields[name]["value"])
                        fields[name]["value"] = d if d is not None else fields[name]["value"]
                    rendered[name] = render_field(name, spec, fields[name]["value"])
        missing = sorted(n for n in used_fields if n != "ext" and not rendered.get(n))
        meta = [n for n in used_fields if rendered.get(n) and n not in NOT_METADATA and not (n == "reel" and fields["reel"]["source"].startswith("card fingerprint"))]
        row: Dict[str, Any] = {"clip_id": c["clip_id"], "structure": c["structure"], "from": c["path"], "to": c["path"], "status": "unchanged",
                               "fields": {n: {"value": rendered.get(n) or None, "source": fields[n]["source"]} for n in sorted(fields) if n in used_fields and n != "ext"}
                                         | {"ext": {"value": fields["ext"]["value"], "source": fields["ext"]["source"]}},
                               "missing": missing, "files": [], "counter": None, "reason": ""}
        rows.append(row)
        if refusal:
            row["status"], row["reason"] = "skipped", refusal
            continue
        if mode == "in_place" and c["structure"] != "generic":
            row["status"], row["reason"] = "skipped", (f"part of a {c['structure']} card structure: its index files name the clip, so renaming it in place "
                                                       "would break the card for NLEs; use --copy-to for renamed copies")
            continue
        if c.get("growing"):
            row["status"], row["reason"] = "skipped", "a recording still being written (growing): renamed only once sealed"
            continue
        if not meta:
            row["reason"] = "no metadata for this clip: no field of the template has a value besides seq / orig / ext, so its name is kept"
            continue
        stem0 = compose(parts, rendered)
        exts = [(rel, role, rem) for rel, role, rem in files]
        spanned = len([f for f in files if f[1] == "media"]) > 1
        longest = max(len((rem + PurePosixPath(rel).suffix.lower()).encode("utf-8")) + (4 if spanned else 0) for rel, role, rem in exts)
        stem = safe_stem(stem0, longest)
        if not stem:
            row["reason"] = "the template gives an empty name for this clip; its name is kept"
            continue
        target_dir = dest if dest is not None else root / first.parent
        tkey = str(target_dir)
        if tkey not in taken:
            taken[tkey] = _taken_in(target_dir)
        own = {_key(PurePosixPath(rel).name) for rel, _, _ in files} if dest is None else set()

        def names(s: str) -> List[Tuple[str, str, str]]:
            out = []
            parts_n = 0
            for rel, role, rem in files:
                ext = PurePosixPath(rel).suffix.lower()
                if role == "media":
                    parts_n += 1
                    out.append((rel, role, f"{s}_p{parts_n:02d}{ext}" if spanned else f"{s}{ext}"))
                else:
                    out.append((rel, role, f"{s}{rem}{ext}"))
            return out
        counter = 1
        candidate = names(stem)
        while any(_key(n) in taken[tkey] and _key(n) not in own for _, _, n in candidate) or len({_key(n) for _, _, n in candidate}) < len(candidate):
            counter += 1
            candidate = names(f"{stem}_{counter}")
        for _, _, n in candidate:
            taken[tkey].add(_key(n))
        prefix = "" if dest is not None or str(first.parent) == "." else str(first.parent) + "/"
        row["files"] = [{"from": rel, "to": prefix + n, "role": role} for rel, role, n in candidate]
        row["to"] = row["files"][0]["to"]
        row["counter"] = counter if counter > 1 else None
        if all(f["from"] == f["to"] for f in row["files"]) and dest is None:
            row["status"], row["reason"] = "unchanged", "the template gives the name it already has"
            row["files"] = []
        else:
            row["status"] = "rename" if dest is None else "copy"
            if counter > 1:
                row["reason"] = f"the name was taken: counter _{counter} appended"
    rows.sort(key=lambda r: r["from"])
    body = {"root": str(root), "mode": mode, "copy_to": str(dest) if dest else None, "template": template,
            "files": [[f["from"], f["to"], *_stat(root / f["from"])] for r in rows for f in r["files"]]}
    plan_id = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("rename", "copy", "unchanged", "skipped")}
    warnings = [f"{{{n}}} has no value for any clip" for n in sorted(used_fields - {"ext"}) if all(n in r["missing"] for r in rows)] if rows else []
    path_warn = [f["to"] for r in rows for f in r["files"] if len(str((dest or root) / f["to"])) > 259]
    if path_warn:
        warnings.append(f"{len(path_warn)} new path(s) are longer than 259 characters: Windows tools without long-path support cannot open them")
    if refusal:
        warnings.append(refusal)
    return {"schema": PLAN_SCHEMA, "ok": True, "root": str(root), "mode": mode, "copy_to": str(dest) if dest else None, "template": template,
            "plan_id": plan_id, "rows": rows, "counts": counts, "files_to_move": sum(len(r["files"]) for r in rows),
            "reel": reel, "manifest": str(root / MANIFEST_NAME) if manifest else None, "warnings": warnings + inv["warnings"],
            "applied": False, "generated_at": _now(), "tool": f"ingest-skill {__version__}"}


def _folder_files(root: Path) -> List[str]:
    from .core import scan
    files, _ = scan(root)
    return [f.relative_to(root).as_posix() for f in files]


def _stat(p: Path) -> List[Any]:
    try:
        st = p.stat()
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return [None, None]


# ---------------------------------------------------------------- apply / undo

def _undo_path(root: Path) -> Path:
    folder = root / RENAME_FOLDER
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return folder / f"{stamp}.json"


def _write_json(path: Path, doc: Dict) -> None:
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _rename(src: Path, dst: Path) -> None:
    """src → dst, never onto an existing file. A change of case only goes through a temporary name (case-insensitive file systems)."""
    if os.path.lexists(dst) and not (_key(src.name) == _key(dst.name) and src.parent == dst.parent):
        raise IngestError("RENAME_REFUSED", f"{dst} exists: never overwritten")
    if _key(src.name) == _key(dst.name):
        tmp = src.with_name(f".{src.name}.ingest-rename")
        os.rename(src, tmp)
        os.rename(tmp, dst)
    else:
        os.rename(src, dst)


def _check_hashes(root: Path, moves: List[Dict], manifest: Dict) -> Tuple[Dict[str, Dict[str, str]], List[Dict]]:
    algo = manifest["hash"]
    fmt = MHL_FORMAT_FOR[algo]
    require([algo])
    recorded = {e["path"]: e for e in manifest["files"]}
    digests: Dict[str, Dict[str, str]] = {}
    bad = []
    for m in moves:
        src = root / m["from"]
        got = hash_file(src, {algo, fmt})
        digests[m["from"]] = got
        e = recorded.get(m["from"])
        if e is not None and got[algo] != e[algo]:
            bad.append({"code": "VERIFY_MISMATCH", "path": m["from"], "hash": algo, "expected": e[algo], "actual": got[algo], "against": MANIFEST_NAME})
    return digests, bad


def _remap_manifest(root: Path, manifest: Dict, mapping: Dict[str, str], entry: Dict, reel: Dict) -> None:
    doc = dict(manifest)
    doc["files"] = [dict(e, path=mapping.get(e["path"], e["path"])) for e in manifest["files"]]
    doc["renames"] = list(manifest.get("renames") or []) + [entry]   # optional key: readers of ingest/manifest@1 that do not know it ignore it
    if not doc.get("card") and reel.get("id"):   # keep the reel the folder had: a fingerprint or clip-name reel changes with the names
        doc["card"] = {"reel": reel["id"], "reel_source": reel["source"]}
    _write_json(root / MANIFEST_NAME, doc)


def _mhl_generation(root: Path, moved: List[Dict], digests: Dict[str, Dict[str, str]], algo: str, started: datetime, process: str,
                    previous: bool) -> Tuple[Optional[str], List[str]]:
    if not mhl.has_history(root) and previous:
        return None, []
    hist = mhl.read_history(root)
    if hist.problems:
        return None, ["no ASC MHL generation written: the folder's ASC MHL history has problems (verify reports them)"]
    fmt = MHL_FORMAT_FOR[algo]
    action = "verified" if previous else "original"
    items = []
    for m in moved:
        p = root / m["to"]
        st = p.stat()
        d = digests[m["from"]]
        item = {"path": m["to"], "size": st.st_size, "mtime": _iso(st.st_mtime), "hashes": {fmt: (d[fmt], action)}}
        if previous:
            item["previous_path"] = m["from"]
        items.append(item)
    if not items:
        return None, []
    try:
        path, _ = mhl.write_generation(hist, items, started, __version__, process=process)
    except (OSError, ValueError) as e:
        return None, [f"writing the ASC MHL generation failed: {e}"]
    return str(path), []


def apply(folder: str, plan_id: str, template: str = DEFAULT_TEMPLATE, values: Optional[Dict[str, str]] = None, fields_file: Optional[str] = None,
          utc_offset: Optional[str] = None, date_from_mtime: bool = False, copy_to: Optional[str] = None) -> Dict:
    """Run the plan a preview showed (`plan_id`); refused (PLAN_CHANGED, nothing done) when the folder or the options now give another one."""
    p = plan(folder, template, values, fields_file, utc_offset, date_from_mtime, copy_to)
    if not plan_id or plan_id != p["plan_id"]:
        raise IngestError("PLAN_CHANGED", f"the plan is now {p['plan_id']}, not {plan_id!r}: the folder or the options changed since the preview "
                                          "(or no preview was made); run `ingest rename plan` again and apply its plan_id")
    root = Path(p["root"])
    moves = [f for r in p["rows"] for f in r["files"]]
    started = datetime.now(timezone.utc)
    report: Dict[str, Any] = {"schema": REPORT_SCHEMA, "action": "apply", "mode": p["mode"], "root": str(root), "copy_to": p["copy_to"], "plan_id": p["plan_id"],
                              "rows": p["rows"], "counts": p["counts"], "moved": 0, "undo": None, "manifest": None, "mhl": None, "failures": [],
                              "warnings": list(p["warnings"]), "started_at": _iso(started.timestamp())}
    if not moves:
        report.update(ok=True, finished_at=_now(), detail="nothing to rename")
        return report
    if p["mode"] == "copy":
        return _apply_copy(p, root, moves, started, report)
    _, manifest = load_manifest(str(root))
    digests, bad = _check_hashes(root, moves, manifest)
    if bad:
        report.update(ok=False, failures=bad, finished_at=_now(),
                      detail="a file differs from its manifest hash: nothing was renamed (run ingest verify)")
        return report
    undo = _undo_path(root)
    record = {"schema": UNDO_SCHEMA, "status": "pending", "mode": "in_place", "root": str(root), "plan_id": p["plan_id"], "template": p["template"],
              "created_at": _now(), "tool": f"ingest-skill {__version__}", "hash": manifest["hash"],
              "entries": [{"from": m["from"], "to": m["to"], "role": m["role"], manifest["hash"]: digests[m["from"]][manifest["hash"]],
                           "size": (root / m["from"]).stat().st_size} for m in moves]}
    _write_json(undo, record)   # before the first rename: an interrupted run can be undone
    done: List[Dict] = []
    try:
        for m in moves:
            dst = root / m["to"]
            _rename(root / m["from"], dst)
            done.append(m)
    except (OSError, IngestError) as e:
        for m in reversed(done):   # put back what was renamed; nothing else changed yet
            try:
                _rename(root / m["to"], root / m["from"])
            except (OSError, IngestError):
                pass
        record["status"] = "rolled_back"
        _write_json(undo, record)
        report.update(ok=False, failures=[{"code": "RENAME_REFUSED", "detail": str(e)}], finished_at=_now(),
                      detail="a rename failed: the files renamed so far were put back")
        return report
    mapping = {m["from"]: m["to"] for m in moves}
    rel_undo = undo.relative_to(root).as_posix()
    _remap_manifest(root, manifest, mapping, {"at": _now(), "action": "rename", "undo": rel_undo, "files": len(moves), "plan_id": p["plan_id"]}, p["reel"])
    mhl_path, warns = _mhl_generation(root, moves, digests, manifest["hash"], started, "in-place", previous=True)
    record["status"] = "applied"
    record["mhl"] = mhl_path
    _write_json(undo, record)
    report.update(ok=True, moved=len(moves), undo=str(undo), manifest=str(root / MANIFEST_NAME), mhl=mhl_path, finished_at=_now())
    report["warnings"] += warns
    return report


def _apply_copy(p: Dict, root: Path, moves: List[Dict], started: datetime, report: Dict) -> Dict:
    dest = Path(p["copy_to"])
    dest.mkdir(parents=True, exist_ok=True)
    manifest: Optional[Dict] = None
    if (root / MANIFEST_NAME).is_file():
        _, manifest = load_manifest(str(root))
    algo = manifest["hash"] if manifest else "sha256"
    fmt = MHL_FORMAT_FOR[algo]
    require([algo])
    recorded = {e["path"]: e[algo] for e in (manifest or {}).get("files", [])}
    existing: Optional[Dict] = None
    if (dest / MANIFEST_NAME).is_file():
        _, existing = load_manifest(str(dest))
        if existing["hash"] != algo:
            raise IngestError("RENAME_REFUSED", f"{dest / MANIFEST_NAME} uses {existing['hash']}, this folder {algo}: copy into another folder")
    digests: Dict[str, Dict[str, str]] = {}
    copied: List[Dict] = []
    for m in moves:
        dst = dest / m["to"]
        if os.path.lexists(dst):
            report["failures"].append({"code": "RENAME_REFUSED", "path": m["to"], "detail": f"{dst} exists: never overwritten"})
            continue
        try:
            d = _copy_verified(root / m["from"], dst, sorted({algo, fmt}))
        except (OSError, IngestError) as e:
            report["failures"].append({"code": getattr(e, "code", "VERIFY_MISMATCH"), "path": m["from"], "detail": str(e)})
            continue
        if m["from"] in recorded and d[algo] != recorded[m["from"]]:
            dst.unlink()
            report["failures"].append({"code": "VERIFY_MISMATCH", "path": m["from"], "hash": algo, "expected": recorded[m["from"]], "actual": d[algo], "against": MANIFEST_NAME})
            continue
        digests[m["from"]] = d
        copied.append(m)
    files = list((existing or {}).get("files", [])) + [{"path": m["to"], "size": (dest / m["to"]).stat().st_size, algo: digests[m["from"]][algo],
                                                         "mtime": _iso((dest / m["to"]).stat().st_mtime)} for m in copied]
    doc = {"schema": MANIFEST_SCHEMA, "tool": f"ingest-skill {__version__}", "source": str(root), "created_at": (existing or {}).get("created_at") or _now(),
           "hash": algo, "files": files,
           "renamed_from": list((existing or {}).get("renamed_from") or []) + [{"path": m["to"], "from": m["from"], "source": str(root)} for m in copied]}
    if copied:
        _write_json(dest / MANIFEST_NAME, doc)
        mhl_path, warns = _mhl_generation(dest, copied, digests, algo, started, "transfer", previous=False)
        report["warnings"] += warns
        undo = _undo_path(dest)
        _write_json(undo, {"schema": UNDO_SCHEMA, "status": "applied", "mode": "copy", "root": str(root), "copy_to": str(dest), "plan_id": p["plan_id"],
                           "created_at": _now(), "tool": f"ingest-skill {__version__}", "hash": algo,
                           "entries": [{"from": m["from"], "to": m["to"], "role": m["role"], algo: digests[m["from"]][algo]} for m in copied],
                           "note": "the folder was only read: its names never changed, so there is nothing to undo there; the copies are listed here"})
        report.update(undo=str(undo), manifest=str(dest / MANIFEST_NAME), mhl=mhl_path)
    report.update(ok=not report["failures"], moved=len(copied), finished_at=_now())
    return report


def latest_undo(root: Path) -> Path:
    folder = root / RENAME_FOLDER
    for p in sorted(folder.glob("*.json"), reverse=True) if folder.is_dir() else []:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("schema") == UNDO_SCHEMA and doc.get("status") == "applied" and doc.get("mode") == "in_place":
            return p
    raise IngestError("UNDO_INVALID", f"no applied in-place rename to undo in {folder}")


def undo(folder: str, undo_file: Optional[str] = None) -> Dict:
    """Rename every file of an applied rename back to its original name (each re-hashed first), then the manifest and an ASC MHL
    generation, as for apply. Refused, with nothing changed, when a renamed file is gone or changed or an original name is taken."""
    root = Path(folder).resolve()
    if not root.is_dir():
        raise IngestError("SOURCE_NOT_FOUND", f"{folder} is not a directory")
    path = Path(undo_file).resolve() if undo_file else latest_undo(root)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise IngestError("UNDO_INVALID", f"{path}: {e}") from None
    if not isinstance(rec, dict) or rec.get("schema") != UNDO_SCHEMA or not isinstance(rec.get("entries"), list):
        raise IngestError("UNDO_INVALID", f"{path} is not an {UNDO_SCHEMA} document")
    if rec.get("mode") != "in_place":
        raise IngestError("UNDO_INVALID", f"{path} records copies (--copy-to): the folder's own names never changed, so there is nothing to restore")
    if rec.get("status") != "applied":
        raise IngestError("UNDO_INVALID", f"{path} is {rec.get('status')!r}, not an applied rename")
    if Path(rec.get("root") or "").resolve() != root:
        raise IngestError("UNDO_INVALID", f"{path} belongs to {rec.get('root')}, not {root}")
    _, manifest = load_manifest(str(root))
    algo = rec.get("hash") or manifest["hash"]
    fmt = MHL_FORMAT_FOR[algo]
    require([algo])
    moves = [{"from": e["to"], "to": e["from"], "role": e.get("role", "media")} for e in rec["entries"]]
    problems = []
    digests: Dict[str, Dict[str, str]] = {}
    for e, m in zip(rec["entries"], moves):
        cur, orig = root / m["from"], root / m["to"]
        if not cur.is_file():
            problems.append({"code": "FILE_MISSING", "path": m["from"]})
            continue
        if os.path.lexists(orig) and _key(orig.name) != _key(cur.name):
            problems.append({"code": "RENAME_REFUSED", "path": m["to"], "detail": "the original name is taken: never overwritten"})
            continue
        got = hash_file(cur, {algo, fmt})
        digests[m["from"]] = got
        if got[algo] != e.get(algo):
            problems.append({"code": "VERIFY_MISMATCH", "path": m["from"], "hash": algo, "expected": e.get(algo), "actual": got[algo], "against": str(path)})
    started = datetime.now(timezone.utc)
    report: Dict[str, Any] = {"schema": REPORT_SCHEMA, "action": "undo", "mode": "in_place", "root": str(root), "copy_to": None, "plan_id": rec.get("plan_id"),
                              "rows": [], "counts": {}, "moved": 0, "undo": str(path), "manifest": None, "mhl": None, "failures": problems, "warnings": [],
                              "started_at": _iso(started.timestamp())}
    if problems:
        report.update(ok=False, finished_at=_now(), detail="nothing was renamed back")
        return report
    done: List[Dict] = []
    try:
        for m in moves:
            _rename(root / m["from"], root / m["to"])
            done.append(m)
    except (OSError, IngestError) as e:
        for m in reversed(done):
            try:
                _rename(root / m["to"], root / m["from"])
            except (OSError, IngestError):
                pass
        report.update(ok=False, failures=[{"code": "RENAME_REFUSED", "detail": str(e)}], finished_at=_now(), detail="a rename failed: the files renamed back so far were put again")
        return report
    _remap_manifest(root, manifest, {m["from"]: m["to"] for m in moves},
                    {"at": _now(), "action": "undo", "undo": path.relative_to(root).as_posix() if str(path).startswith(str(root)) else str(path), "files": len(moves),
                     "plan_id": rec.get("plan_id")}, {})
    mhl_path, warns = _mhl_generation(root, moves, digests, algo, started, "in-place", previous=True)
    rec["status"] = "undone"
    rec["undone_at"] = _now()
    _write_json(path, rec)
    report.update(ok=True, moved=len(moves), manifest=str(root / MANIFEST_NAME), mhl=mhl_path, finished_at=_now(),
                  rows=[{"from": m["from"], "to": m["to"], "role": m["role"]} for m in moves])
    report["warnings"] += warns
    return report
