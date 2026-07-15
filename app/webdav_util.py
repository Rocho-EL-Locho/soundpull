"""Thin helpers around the webdav4 client (connection, listing, file operations)."""
from __future__ import annotations

import io
import logging
import posixpath
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from httpx import URL
from webdav4.client import Client

from app.config import settings

log = logging.getLogger("webdav_util")

# webdav4 passes no timeout, so httpx falls back to its 5s default — far too tight for uploading
# multi-MB audio files over a remote WebDAV, where a single slow PUT/response raises
# `httpx.ReadTimeout: The read operation timed out` and (in an artist run) aborts the whole job.
# Generous read/write budgets for large transfers; a short connect so an unreachable host fails fast.
_WEBDAV_TIMEOUT = httpx.Timeout(connect=30.0, read=180.0, write=180.0, pool=30.0)


def _encode_webdav_path(path: str) -> str:
    """Percent-encode the URL-reserved chars httpx's path validator rejects.

    webdav4 feeds each resource path straight into httpx's ``URL.copy_with(path=…)``,
    whose path-component regex is ``[^?#]*``. A literal ``#`` or ``?`` in a folder or
    file name — both legal on disk and on the server, and common in track/album titles
    (e.g. ``Best Of #1``, ``What? EP``) — therefore raises
    ``InvalidURL: Invalid URL component 'path'`` and aborts the whole WebDAV upload
    (seen on artist downloads). Pre-encode them (and ``%`` first, so an existing ``%XX``
    in a name isn't misread as an escape); httpx leaves valid ``%XX`` sequences intact
    and the server decodes them back, so the stored name is unchanged. ``/`` separators
    are untouched, so this is a no-op for any path without ``%``/``#``/``?``.
    """
    return path.replace("%", "%25").replace("#", "%23").replace("?", "%3F")


class _SafePathClient(Client):
    """webdav4 client that percent-encodes reserved chars in every resource path.

    All path-taking operations (``ls``/``exists``/``mkdir``/``upload_file`` …) route
    through ``join_url``, so encoding here fixes them centrally. Response-key matching
    still works because httpx decodes ``URL.path`` on both the request and href sides.
    """

    def join_url(self, path: str, add_trailing_slash: bool = False) -> URL:
        return super().join_url(
            _encode_webdav_path(path), add_trailing_slash=add_trailing_slash
        )


def _check_host_allowed(url: str) -> None:
    """SSRF guard: reject targets outside WEBDAV_ALLOWED_HOSTS (if configured)."""
    allow = settings.webdav_host_allowlist
    if not allow:
        return
    host = (urlparse(url).hostname or "").lower()
    if host not in allow:
        raise ValueError(f"WebDAV-Host nicht erlaubt: {host or '(leer)'}")


def make_client(url: str, username: str | None, password: str | None) -> Client:
    _check_host_allowed(url)
    return _SafePathClient(base_url=url, auth=(username or "", password or ""),
                           timeout=_WEBDAV_TIMEOUT)


def list_dirs(client: Client, path: str) -> list[tuple[str, str]]:
    """Return (display_name, full_relative_path) for sub-directories of `path`."""
    entries = client.ls(path or "", detail=True)
    dirs: list[tuple[str, str]] = []
    for e in entries:
        if isinstance(e, dict) and e.get("type") == "directory":
            full = str(e.get("name", "")).rstrip("/")
            if not full:
                continue
            dirs.append((full.split("/")[-1], full))
    return sorted(dirs, key=lambda t: t[0].lower())


# --- Path safety -----------------------------------------------------------

def resolve_rel(rel: str) -> str:
    """Validate a library-relative POSIX path; return it normalised.

    Rejects — BEFORE any network call — absolute paths, empty paths and `..` traversal, so a
    file operation can never escape the user's WebDAV base folder (roadmap 01). The check is
    on the RAW segments (not post-`normpath`, which would collapse ``a/../b`` to ``b`` and mask
    an escape); a `.` or double-slash segment is dropped, a `..` segment or leading `/` raises.

    Also rejects backslashes and control characters: WebDAV paths are URL paths where `/` is
    the only separator, so `\\` is a literal filename char here — but some servers/backends
    normalise it to `/`, which would turn ``..\\..`` into traversal. Refusing both keeps the
    guard robust regardless of the server's path handling (defense-in-depth).
    """
    raw = (rel or "").strip()
    if not raw:
        raise ValueError("Leerer Pfad ist nicht erlaubt.")
    if raw.startswith("/"):
        raise ValueError(f"Absoluter Pfad ist nicht erlaubt: {rel!r}")
    if "\\" in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError(f"Unzulässige Zeichen im Pfad: {rel!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"Unzulässiger Pfad (Traversal): {rel!r}")
    return posixpath.normpath("/".join(parts))


# --- Transient-retry policy (shared by uploads and file ops) ---------------

_TRANSIENT_ATTEMPTS = 4
_TRANSIENT_BACKOFF_BASE = 2.0  # seconds; exponential waits between attempts: 2, 4, 8


def retry_transient(fn, *, desc: str):
    """Call `fn`, retrying TRANSIENT network failures (timeout / transport error) with bounded
    EXPONENTIAL backoff (2/4/8s), then re-raising. A non-transient error (bad path, auth, 4xx)
    is NOT retried — it re-raises immediately. Shared policy so uploads (`pipeline`) and the
    file-ops primitives below use the same behaviour instead of duplicating it (issue #40)."""
    for attempt in range(1, _TRANSIENT_ATTEMPTS + 1):
        try:
            return fn()
        except httpx.TransportError as exc:  # timeout / connect / read / write / network
            if attempt == _TRANSIENT_ATTEMPTS:
                raise
            delay = _TRANSIENT_BACKOFF_BASE * 2 ** (attempt - 1)  # exponential: 2, 4, 8 …
            log.warning("WebDAV %s failed (attempt %d/%d): %s — retrying in %.0fs",
                        desc, attempt, _TRANSIENT_ATTEMPTS, exc, delay)
            time.sleep(delay)


# --- File-operation primitives ---------------------------------------------
#
# Thin wrappers over webdav4's own operations, with the shared transient retry. Callers
# pass paths that have already gone through `resolve_rel` (+ the user's `webdav_folder`
# prefix); these helpers do NOT re-validate, so the index-aware layer (`app.library_ops`)
# owns the safety boundary.

def ensure_remote_dir(client: Client, posix_dir: str) -> None:
    """Create `posix_dir` and every missing parent (idempotent, race-tolerant)."""
    parts = [p for p in posix_dir.split("/") if p]
    cumulative = ""
    for part in parts:
        cumulative = f"{cumulative}/{part}" if cumulative else part
        try:
            if not client.exists(cumulative):
                client.mkdir(cumulative)
        except Exception:
            # Race / already-exists on some servers — verify and continue.
            if not client.exists(cumulative):
                raise


def path_exists(client: Client, remote_path: str) -> bool:
    return bool(retry_transient(lambda: client.exists(remote_path),
                                desc=f"exists {remote_path!r}"))


def download_file(client: Client, remote_path: str, local_path: Path) -> None:
    """Download a remote file into `local_path` (parent dirs created)."""
    local = Path(local_path)
    local.parent.mkdir(parents=True, exist_ok=True)
    retry_transient(lambda: client.download_file(remote_path, str(local)),
                    desc=f"download {remote_path!r}")


def read_text(client: Client, remote_path: str, *, encoding: str = "utf-8") -> str:
    """Download a small text file (e.g. an `.m3u8`) into memory and decode it (roadmap 04)."""
    buf = io.BytesIO()
    retry_transient(lambda: client.download_fileobj(remote_path, buf),
                    desc=f"read {remote_path!r}")
    return buf.getvalue().decode(encoding)


def write_text(client: Client, remote_path: str, text: str, *, encoding: str = "utf-8") -> None:
    """Upload `text` to `remote_path`, overwriting (roadmap 04 — playlist m3u repair).

    Mirrors the in-memory upload frame `library_index` uses for `.lrc` sidecars
    (`client.upload_fileobj(BytesIO(...), …, overwrite=True)`) — no temp file on disk.
    """
    retry_transient(
        lambda: client.upload_fileobj(io.BytesIO(text.encode(encoding)), remote_path,
                                      overwrite=True),
        desc=f"write {remote_path!r}")


def delete_path(client: Client, remote_path: str) -> None:
    retry_transient(lambda: client.remove(remote_path), desc=f"delete {remote_path!r}")


def move_path(client: Client, src: str, dst: str, *, overwrite: bool = False) -> None:
    """Move/rename `src` to `dst`, creating the destination's parent dirs first."""
    parent = posixpath.dirname(dst)
    if parent:
        ensure_remote_dir(client, parent)
    retry_transient(lambda: client.move(src, dst, overwrite=overwrite),
                    desc=f"move {src!r} -> {dst!r}")
