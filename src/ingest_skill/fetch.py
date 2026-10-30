"""`ingest fetch <url>`: download one video URL with yt-dlp, then offload it like a card (ADR-005).

- yt-dlp is an optional extra (`pip install 'ingest-skill[fetch]'`), used as a library: the URL and every option are
  Python values handed to `yt_dlp.YoutubeDL`, never a command line, so nothing is shell-interpolated and a URL can
  never be read as an option. Without the extra, fetch is refused as FETCH_UNAVAILABLE before anything is written.
- The download lands in a staging folder (a new temporary folder, or one under `--staging`). That folder is then the
  source of an ordinary `offload`: the same single read, verified writes, read-back, ingest-manifest.json, ASC MHL
  history and inventory as a camera card, so `ingest verify` works on fetched material exactly as on a card.
- What is recorded (the "fetch" key of the manifest, the report and the inventory): the URL as given and yt-dlp's
  page URL, both with credentials removed (userinfo, and the values of token / signature / key / password-like query,
  fragment and path parameters); the extractor; the yt-dlp version; the format selector and the selected format id;
  the video id, title, duration and live flags; the times. Never a cookie, a request header, or the media URL
  yt-dlp downloaded from (those are signed CDN links).
- No cookies, logins, .netrc or browser cookies are used, and no DRM option exists: protected media are refused by
  yt-dlp itself and reported as FETCH_FAILED. The user must have the right to download what they fetch.
"""
from __future__ import annotations

import hashlib
import importlib
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from . import cards
from .core import _inside, check_destinations, offload
from .errors import IngestError
from .hashing import DEFAULT_HASH

INSTALL_HINT = "pip install 'ingest-skill[fetch]'"
FETCH_SCHEMA = "ingest/fetch@1"
# Video + audio merged into one file (yt-dlp's own default when ffmpeg is available, and the selector the agent used).
FORMAT_SELECTOR = "bestvideo*+bestaudio/best"
# Without ffmpeg nothing can be merged: the best single file that already has both streams.
FORMAT_SELECTOR_NO_FFMPEG = "best"
MERGE_FORMAT = "mp4"
SOCKET_TIMEOUT = 60.0
DOWNLOAD_DIR = "_download"   # inside the staging folder; never a valid card name (cards.REEL_SAFE starts alphanumeric)
REDACTED = "REDACTED"
# A query / fragment / path parameter whose name contains one of these (case-insensitive), or is one of the exact
# names below, has its value replaced by REDACTED. Over-redacting a harmless parameter is the accepted cost.
SECRET_PARTS = ("token", "sig", "key", "secret", "passw", "auth", "credential", "session", "cookie", "jwt", "hmac", "policy")
SECRET_NAMES = {"pass", "pwd", "code", "sid", "st", "hdnts", "hdnea", "otp"}
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
_PATH_PARAM = re.compile(r";([^/;=?#]+)=([^/;?#]*)")


def _secret(name: str) -> bool:
    n = unquote_plus(name).strip().lower()
    return n in SECRET_NAMES or any(part in n for part in SECRET_PARTS)


def _redact_params(text: str, sep: str = "&") -> Tuple[str, List[str]]:
    out, removed = [], []
    for piece in text.split(sep):
        name, eq, value = piece.partition("=")
        if eq and value and _secret(name):
            out.append(f"{name}={REDACTED}")
            removed.append(unquote_plus(name))
        else:
            out.append(piece)
    return sep.join(out), removed


def redact_url(url: str) -> Tuple[str, List[str]]:
    """The URL with its credentials removed, and the names (never the values) of what was removed: "userinfo" for a
    user:password@ part, and each secret-looking query, fragment ("#name") and path (";name") parameter."""
    try:
        parts = urlsplit(url)
        host = parts.netloc
    except ValueError:
        return "<unparseable URL>", ["url"]
    removed: List[str] = []
    if "@" in host:
        host = host.rsplit("@", 1)[1]
        removed.append("userinfo")

    def path_param(m: "re.Match[str]") -> str:
        if m.group(2) and _secret(m.group(1)):
            removed.append(";" + unquote_plus(m.group(1)))
            return f";{m.group(1)}={REDACTED}"
        return m.group(0)

    path = _PATH_PARAM.sub(path_param, parts.path)
    query, q_removed = _redact_params(parts.query) if parts.query else ("", [])
    fragment, f_removed = _redact_params(parts.fragment) if "=" in parts.fragment else (parts.fragment, [])
    return urlunsplit((parts.scheme, host, path, query, fragment)), removed + q_removed + ["#" + n for n in f_removed]


def redact_text(text: str) -> str:
    """Every URL inside a message (a yt-dlp error, a warning) with its credentials removed."""
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0))[0], text)


def load_yt_dlp() -> Optional[Any]:
    """The yt_dlp package, or None when the optional extra is not installed."""
    try:
        return importlib.import_module("yt_dlp")
    except ImportError:
        return None


def yt_dlp_version(mod: Any) -> str:
    try:
        return str(importlib.import_module("yt_dlp.version").__version__)
    except (ImportError, AttributeError):
        return str(getattr(mod, "__version__", "unknown"))


def fetch_check() -> Dict[str, Any]:
    """The `ingest doctor` entry for yt-dlp: ok with its version, or unavailable (not a failure: offload, verify and
    inventory do not need it)."""
    mod = load_yt_dlp()
    if mod is None:
        return {"status": "unavailable", "version": None,
                "detail": f"optional package not installed: ingest fetch is UNAVAILABLE (refused, nothing written); {INSTALL_HINT}"}
    version = yt_dlp_version(mod)
    merge = "ffmpeg on PATH: video and audio are merged into one file" if shutil.which("ffmpeg") else \
        f"ffmpeg not on PATH: fetch downloads the best single file with both streams (format {FORMAT_SELECTOR_NO_FFMPEG!r}), nothing is merged"
    return {"status": "ok", "version": version, "detail": f"yt-dlp {version}: ingest fetch available; {merge}"}


def check_url(url: str) -> str:
    """An http(s) URL with a host; anything else is URL_INVALID. Returns the redacted form for messages."""
    shown, _ = redact_url(url)
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        host = None
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not host or any(c.isspace() for c in url):
        raise IngestError("URL_INVALID", f"{shown}: fetch takes one http:// or https:// URL")
    return shown


def check_name(name: Optional[str]) -> Optional[str]:
    if name is not None and not cards.REEL_SAFE.match(name):
        raise IngestError("NAME_INVALID", f"name {name!r}: use 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit")
    return name


def default_name(info: Dict[str, Any], shown_url: str) -> str:
    """<extractor>-<video id> as one safe folder name, else FETCH-<first 8 hex of the sha256 of the redacted URL>."""
    raw = f"{info.get('extractor') or 'fetch'}-{info.get('id') or ''}"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")[:64].rstrip("._-")
    if not info.get("id") or not cards.REEL_SAFE.match(name):
        name = "FETCH-" + hashlib.sha256(shown_url.encode("utf-8")).hexdigest()[:8]
    return name


class _Log:
    """yt-dlp's logger: nothing reaches stdout (the --json document is the only thing printed there); warnings are kept,
    with any URL in them redacted."""

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def debug(self, msg: str) -> None:
        pass

    info = debug

    def warning(self, msg: str) -> None:
        self.warnings.append(redact_text(str(msg)))

    def error(self, msg: str) -> None:
        self.warnings.append(redact_text(str(msg)))


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _download(mod: Any, url: str, out_dir: Path, selector: str, log: _Log) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """Metadata first (live or not, decided by yt-dlp's own is_live, never guessed), then the download into out_dir.
    Returns (metadata, download info, live_from_start). Every yt-dlp failure is FETCH_FAILED, its message redacted."""
    base: Dict[str, Any] = {
        "quiet": True, "noprogress": True, "logger": log, "noplaylist": True,
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"), "format": selector, "merge_output_format": MERGE_FORMAT,
        "socket_timeout": SOCKET_TIMEOUT,
        # Stated, not left to defaults: no cookies or credentials are read or written, nothing but the media is
        # written, and protected (DRM) formats stay unplayable, as yt-dlp's own default has it.
        "cookiefile": None, "cookiesfrombrowser": None, "usenetrc": False,
        "writeinfojson": False, "writethumbnail": False, "writesubtitles": False, "writeautomaticsub": False,
        "allow_unplayable_formats": False,
    }
    try:
        with mod.YoutubeDL(dict(base)) as ydl:
            meta = ydl.extract_info(url, download=False)
        _single(meta)
        live = bool(meta.get("is_live"))
        # An in-progress stream is captured from its beginning, not from "now"; an ended one downloads like any video.
        with mod.YoutubeDL(dict(base, live_from_start=True) if live else dict(base)) as ydl:
            info = ydl.extract_info(url, download=True)
        _single(info)
    except IngestError:
        raise
    except Exception as e:  # noqa: BLE001 - yt-dlp's DownloadError / ExtractorError, or anything else it raised
        raise IngestError("FETCH_FAILED", f"yt-dlp: {redact_text(str(e)) or type(e).__name__}") from None
    return meta, info, live


def _single(info: Any) -> None:
    if not hasattr(info, "get"):   # yt-dlp promises a dictionary-like value, not a dict
        raise IngestError("FETCH_FAILED", "yt-dlp returned no metadata")
    if info.get("_type") in ("playlist", "multi_video"):
        raise IngestError("FETCH_FAILED", "the URL is a playlist or a multi-video page; fetch takes the URL of one video")


def _downloaded_files(info: Dict[str, Any], out_dir: Path) -> List[Path]:
    """The files yt-dlp left in out_dir. Refused when there is none, when a partial download is left, or when a file
    yt-dlp reported is missing or outside out_dir."""
    present = sorted(p for p in out_dir.rglob("*") if p.is_file()) if out_dir.is_dir() else []
    if not present:
        raise IngestError("FETCH_FAILED", "yt-dlp reported success but wrote no file")
    partial = [p.name for p in present if p.name.endswith((".part", ".ytdl")) or ".part-Frag" in p.name]
    if partial:
        raise IngestError("FETCH_FAILED", f"the download is incomplete: {', '.join(partial)}")
    for d in info.get("requested_downloads") or []:
        fp = d.get("filepath") if isinstance(d, dict) else None
        if fp and (not Path(fp).is_file() or not _inside(Path(fp).resolve(), out_dir.resolve())):
            raise IngestError("FETCH_FAILED", f"yt-dlp reported {Path(fp).name}, which is not in the staging folder")
    return present


def fetch(url: str, destinations: Iterable[str], project: Optional[str] = None, name: Optional[str] = None,
          hash_algorithm: str = DEFAULT_HASH, format_selector: Optional[str] = None, staging: Optional[str] = None) -> Dict[str, Any]:
    """Download `url` (one video) into a staging folder, then offload that folder to every destination as
    <dest>/<project>/<name> with the same verified path as a card. `name` defaults to <extractor>-<video id>.
    The staging folder is removed when every destination is verified, and kept (and named in the report) otherwise,
    so the download is never lost. Returns the offload report with the "fetch" record and the "staging" outcome."""
    shown = check_url(url)
    check_name(name)
    destinations = list(destinations)
    check_destinations(destinations, hash_algorithm)
    mod = load_yt_dlp()
    if mod is None:
        raise IngestError("FETCH_UNAVAILABLE", f"ingest fetch needs the optional yt-dlp package, which is not installed: {INSTALL_HINT}")
    version = yt_dlp_version(mod)
    selector = format_selector or (FORMAT_SELECTOR if shutil.which("ffmpeg") else FORMAT_SELECTOR_NO_FFMPEG)
    parent = Path(staging).resolve() if staging else Path(tempfile.gettempdir()).resolve()
    if staging:
        parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="ingest-fetch-", dir=str(parent))).resolve()
    keep = False
    try:
        check_destinations(destinations, hash_algorithm, stage)   # a destination inside the staging folder, or around it
        log = _Log()
        if format_selector is None and selector == FORMAT_SELECTOR_NO_FFMPEG:
            log.warnings.append(f"ffmpeg not on PATH: downloading the best single file with both streams (format {selector!r}); nothing is merged")
        out_dir = stage / DOWNLOAD_DIR
        started = _now()
        meta, info, live = _download(mod, url, out_dir, selector, log)
        _downloaded_files(info, out_dir)
        card_name = name or default_name(info, shown)
        card = stage / card_name
        out_dir.rename(card)
        files = sorted(p.relative_to(card).as_posix() for p in card.rglob("*") if p.is_file())
        page, page_removed = redact_url(str(info.get("webpage_url") or url))
        _, url_removed = redact_url(url)
        record = {
            "schema": FETCH_SCHEMA,
            "source_url": shown,
            "webpage_url": page,
            "redacted": sorted(set(url_removed + page_removed)),
            "extractor": _text(info.get("extractor")),
            "extractor_key": _text(info.get("extractor_key")),
            "downloader": {"name": "yt-dlp", "version": version},
            "format_selector": selector,
            "format_id": _text(info.get("format_id")),
            "id": str(info.get("id") or ""),
            "title": _text(info.get("title")),
            "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
            "is_live": bool(meta.get("is_live")),
            "was_live": bool(meta.get("was_live")),
            "live_from_start": live,
            "files": files,
            "started_at": started,
            "fetched_at": _now(),
        }
        report = offload(str(card), destinations, project=project, hash_algorithm=hash_algorithm, fetch=record)
        doc = report.to_dict()
        keep = not report.ok
        doc["schema"] = "ingest/fetch-report@1"
        doc["staging"] = {"path": str(stage), "kept": keep,
                          "detail": "kept: not every destination is verified; the download is still here" if keep else "removed: every destination is verified"}
        doc["warnings"] = log.warnings
        return doc
    finally:
        if not keep:
            shutil.rmtree(stage, ignore_errors=True)
