"""Proxies queued at ingest: `ingest offload --proxy` (ECOSYSTEM_ROADMAP.md step 11, ADR-010).

Once a clip's file is verified on a destination, it is queued for ffmpeg-skill `proxy` (scripts/proxy.py), which runs
in a worker thread while the next files are copied. The proxy is made from that verified copy (never from the card),
written to <destination>/ingest-proxies/<path>.proxy.mp4, and recorded in ingest-proxies/proxies.json with the
original's hash: the inventory links a clip to its proxy by that hash. A proxy that fails is reported and never fails
the offload. The encoding itself is ffmpeg-skill's: this module decides only which files, where and with what flags.

Hardware encoding: ffmpeg-skill's tools take `--hw` (VideoToolbox, Apple Silicon only). `--hw` is passed when
ffmpeg-skill's own doctor says it can run here (`hw.platform_ok`); its `gpu_encoders` (NVENC, QSV, ...) are only
listed in the report, because no ffmpeg-skill tool uses them.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from . import __version__
from .errors import IngestError
from .hashing import hash_file

FOLDER = "ingest-proxies"
INDEX = "proxies.json"
INDEX_SCHEMA = "ingest/proxies@1"
ENV_DIR = "INGEST_FFMPEG_SKILL_DIR"
DEFAULT_DIR = Path("~/.claude/skills/ffmpeg-skill")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".mkv", ".mpg", ".mpeg"}
NOT_DECODABLE = {".braw": "Blackmagic RAW", ".r3d": "RED R3D", ".crm": "Canon Cinema RAW Light"}
DOCTOR_TIMEOUT_S = 120


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_skill(skill_dir: Optional[str] = None) -> Optional[Path]:
    """The ffmpeg-skill folder: skill_dir, else $INGEST_FFMPEG_SKILL_DIR, else ~/.claude/skills/ffmpeg-skill; None when
    it has no scripts/proxy.py."""
    for cand in (skill_dir, os.environ.get(ENV_DIR), str(DEFAULT_DIR)):
        if cand:
            p = Path(cand).expanduser()
            if (p / "scripts" / "proxy.py").is_file():
                return p.resolve()
            if skill_dir and cand == skill_dir:
                return None   # an explicit folder that is not ffmpeg-skill is not silently replaced by another one
    return None


def require_skill(skill_dir: Optional[str] = None) -> Path:
    p = find_skill(skill_dir)
    if p is None:
        where = skill_dir or os.environ.get(ENV_DIR) or str(DEFAULT_DIR)
        raise IngestError("PROXY_UNAVAILABLE", f"--proxy needs ffmpeg-skill (scripts/proxy.py), not found at {where}: install it (npx ffmpeg-skill) "
                                               f"or pass --ffmpeg-skill DIR / set {ENV_DIR}")
    return p


def skill_doctor(skill: Path) -> Dict:
    """ffmpeg-skill's own doctor (scripts/_contract.py doctor --json): its version, hw and gpu_encoders, or why not."""
    try:
        r = subprocess.run([sys.executable, str(skill / "scripts" / "_contract.py"), "doctor", "--json"], capture_output=True, text=True, timeout=DOCTOR_TIMEOUT_S)
        doc = json.loads(r.stdout)
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
        return doc
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"error": f"ffmpeg-skill doctor did not answer: {e}"}


def check(skill_dir: Optional[str] = None) -> Dict:
    """ingest doctor's view: where ffmpeg-skill is (without running it)."""
    p = find_skill(skill_dir)
    if p is None:
        return {"status": "unavailable", "detail": f"not found ({ENV_DIR} or {DEFAULT_DIR}): offload --proxy is refused (PROXY_UNAVAILABLE); nothing else needs it"}
    return {"status": "ok", "detail": str(p)}


def proxy_path(rel: str) -> str:
    return f"{FOLDER}/{rel}.proxy.mp4"


def eligible(rel: str) -> Tuple[bool, str]:
    ext = PurePosixPath(rel).suffix.lower()
    if ext in NOT_DECODABLE:
        return False, f"{NOT_DECODABLE[ext]} is not decoded by ffmpeg: no proxy"
    return ext in VIDEO_EXTENSIONS, "not a video file"


def read_index(root: Path) -> List[Dict]:
    p = root / FOLDER / INDEX
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict) or doc.get("schema") != INDEX_SCHEMA or not isinstance(doc.get("proxies"), list):
        return []
    return [e for e in doc["proxies"] if isinstance(e, dict) and isinstance(e.get("original_hash"), dict) and e.get("path")]


def write_index(root: Path, entries: List[Dict]) -> Path:
    folder = root / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"schema": INDEX_SCHEMA, "tool": f"ingest-skill {__version__}", "updated_at": _now(),
           "proxies": sorted(entries, key=lambda e: (e["original"], e["original_hash"]["value"]))}
    tmp = folder / (INDEX + ".part")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, folder / INDEX)
    return folder / INDEX


class ProxyQueue:
    """One worker thread running ffmpeg-skill proxy on each verified file in turn, while the offload goes on."""

    def __init__(self, skill: Path, hash_algorithm: str, width: Optional[int] = None):
        self.skill, self.hash_algorithm, self.width = skill, hash_algorithm, width
        doc = skill_doctor(skill)
        hw = doc.get("hw") if isinstance(doc.get("hw"), dict) else {}
        self.hw = hw.get("platform_ok") is True
        self.info = {"skill": "ffmpeg-skill", "tool": "proxy", "skill_dir": str(skill), "version": doc.get("version"),
                     "encoder": "hw" if self.hw else "cpu",
                     "encoder_detail": ("ffmpeg-skill --hw (VideoToolbox): its doctor reports hw.platform_ok" if self.hw else
                                        "CPU (x264): ffmpeg-skill's doctor does not report a hardware encoder its tools can use here"
                                        + (f" ({doc['error']})" if doc.get("error") else "")),
                     "gpu_encoders": doc.get("gpu_encoders")}
        self.items: List[Dict] = []
        self.indexes: Dict[str, List[Dict]] = {}
        self._q: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="ingest-proxy", daemon=True)
        self._thread.start()

    def submit(self, root: Path, rel: str, digest: str) -> None:
        """Queue a proxy of root/rel, the verified copy whose offload hash is digest."""
        with self._lock:
            if str(root) not in self.indexes:
                self.indexes[str(root)] = read_index(root)
        self._q.put((root, rel, digest))

    def skip(self, rel: str, detail: str) -> None:
        self.items.append({"original": rel, "status": "skipped", "detail": detail})

    def _args(self, src: Path, out: Path) -> List[str]:
        args = [str(src), "-o", str(out), "--json"]
        if self.width:
            args += ["--width", str(self.width)]
        if self.hw:
            args.append("--hw")
        return args

    def _one(self, root: Path, rel: str, digest: str) -> Dict:
        item: Dict = {"original": rel, "root": str(root), "original_hash": {"algorithm": self.hash_algorithm, "value": digest}, "path": proxy_path(rel)}
        final = root / item["path"]
        index = self.indexes[str(root)]
        old = next((e for e in index if e["original"] == rel and e["original_hash"].get("value") == digest), None)
        if old is not None and final.is_file() and old.get("sha256") and hash_file(final, ["sha256"])["sha256"] == old["sha256"]:
            return {**item, "status": "exists", "size": old.get("size"), "sha256": old["sha256"], "detail": "made by an earlier run for the same original hash"}
        tmp = final.with_name(f".{final.name[:-4]}.ingest-part.mp4")
        args = self._args(root / rel, tmp)
        item["args"] = [a if a not in (str(root / rel), str(tmp)) else ("<original>" if a == str(root / rel) else "<proxy>") for a in args]
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([sys.executable, str(self.skill / "scripts" / "proxy.py"), *args], capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError) as e:
            return {**item, "status": "failed", "detail": f"ffmpeg-skill proxy did not run: {e}"}
        try:
            result = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        except ValueError:
            result = {}
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            try:
                tmp.unlink()
            except OSError:
                pass
            err = (r.stderr or r.stdout or "").strip().splitlines()
            return {**item, "status": "failed", "detail": f"ffmpeg-skill proxy exited {r.returncode}: {err[-1] if err else 'no output file'}"}
        os.replace(tmp, final)
        hw = result.get("hw") if isinstance(result.get("hw"), dict) else None
        made = {**item, "status": "made", "size": final.stat().st_size, "sha256": hash_file(final, ["sha256"])["sha256"],
                "encoder": ("hw" if hw.get("used") else "cpu") if hw is not None and hw.get("used") is not None else self.info["encoder"], "created_at": _now()}
        with self._lock:
            index[:] = [e for e in index if not (e["original"] == rel and e["original_hash"].get("value") == digest)]
            index.append({k: made[k] for k in ("original", "original_hash", "path", "size", "sha256", "encoder", "created_at")}
                         | {"made_by": {k: self.info[k] for k in ("skill", "tool", "version")}, "args": made["args"]})
        return made

    def _run(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            try:
                self.items.append(self._one(*job))
            except Exception as e:  # noqa: BLE001 - a proxy never fails the offload
                self.items.append({"original": job[1], "root": str(job[0]), "status": "failed", "detail": f"{type(e).__name__}: {e}"})

    def close(self) -> Dict:
        """Wait for the queue, write each destination's proxies.json, and return the report's "proxy" section."""
        self._q.put(None)
        self._thread.join()
        for root, entries in self.indexes.items():
            if entries:
                write_index(Path(root), entries)
        items = sorted(self.items, key=lambda i: i["original"])
        count = {s: sum(1 for i in items if i["status"] == s) for s in ("made", "exists", "failed", "skipped")}
        return {**self.info, **count, "items": items}
