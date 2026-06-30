"""Thin helpers around the webdav4 client (connection + directory listing)."""
from __future__ import annotations

from urllib.parse import urlparse

from webdav4.client import Client

from app.config import settings


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
    return Client(base_url=url, auth=(username or "", password or ""))


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
