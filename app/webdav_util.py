"""Thin helpers around the webdav4 client (connection + directory listing)."""
from __future__ import annotations

from webdav4.client import Client


def make_client(url: str, username: str | None, password: str | None) -> Client:
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
