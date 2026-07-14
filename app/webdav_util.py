"""Thin helpers around the webdav4 client (connection + directory listing)."""
from __future__ import annotations

from urllib.parse import urlparse

import httpx
from httpx import URL
from webdav4.client import Client

from app.config import settings

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
