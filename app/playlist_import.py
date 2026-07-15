"""Spotify / Apple Music playlist import — track-list parsers (roadmap 13).

Reads the **track list** (metadata only) of a public Spotify or Apple Music playlist/album and
returns a neutral `ImportedPlaylist` that feeds feature 12's matcher (`app.matching`). **No audio is
ever fetched from Spotify/Apple** (that's DRM'd and out of bounds) — audio comes from YouTube Music
like every other download.

Trust / SSRF: only a **fixed** set of Spotify/Apple hosts is ever contacted; the pasted URL's host is
validated by exact `urlparse().hostname` membership (so `api.spotify.com.evil.tld` is rejected)
before any request. Every fetch fails soft with a typed error (`SpotifyError` / `AppleParseError`)
carrying a short, stack-free message; the UI shows a translated toast and nothing else breaks. The
Apple path is deliberately fragile (public-page JSON) and isolated to `fetch_apple`.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import settings

log = logging.getLogger("playlist_import")

_TIMEOUT = 20
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")

_SPOTIFY_HOSTS = {"open.spotify.com", "api.spotify.com", "accounts.spotify.com"}
_APPLE_HOSTS = {"music.apple.com"}
_IMPORT_HOSTS = _SPOTIFY_HOSTS | _APPLE_HOSTS

# open.spotify.com/playlist/<id> | /album/<id>   (optionally /intl-xx/ prefix, ?si=… suffix)
_SPOTIFY_PATH = re.compile(r"/(?:intl-[a-z]{2}/)?(playlist|album)/([A-Za-z0-9]+)")
# music.apple.com/<country>/(playlist|album)/<slug>/<id>   (id: pl.u-… for playlists, digits for albums)
_APPLE_PATH = re.compile(r"/[a-z]{2}/(playlist|album)/[^/]+/([A-Za-z0-9.\-]+)")


class PlaylistImportError(Exception):
    """Base: any import failure, surfaced to the UI as a soft warning (never a stack)."""


class SpotifyError(PlaylistImportError):
    pass


class AppleParseError(PlaylistImportError):
    pass


@dataclass(frozen=True)
class ImportedTrack:
    artist: str
    title: str
    album: Optional[str] = None


@dataclass(frozen=True)
class ImportedPlaylist:
    name: str
    source: str            # "spotify" | "apple"
    source_id: str         # stable id for the import folder hash
    tracks: list[ImportedTrack]


# --- URL detection (SSRF-guarded) ------------------------------------------

def detect_import_url(url: str) -> Optional[tuple[str, str, str]]:
    """Return ``(source, kind, id)`` for a supported Spotify/Apple URL, else None.

    Host is checked by exact membership against the fixed allowlist (rejects lookalikes like
    ``api.spotify.com.evil.tld``); scheme must be http(s). Returns None for anything else.
    """
    try:
        parts = urlparse(url or "")
    except Exception:  # noqa: BLE001 - a malformed URL is simply unsupported
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if host not in _IMPORT_HOSTS:
        return None
    if host in _SPOTIFY_HOSTS:
        m = _SPOTIFY_PATH.search(parts.path or "")
        return ("spotify", m.group(1), m.group(2)) if m else None
    m = _APPLE_PATH.search(parts.path or "")
    return ("apple", m.group(1), m.group(2)) if m else None


def fetch_playlist(url: str) -> ImportedPlaylist:
    """Fetch the track list for a supported playlist/album URL. Raises PlaylistImportError."""
    detected = detect_import_url(url)
    if detected is None:
        raise PlaylistImportError("Kein unterstützter Spotify-/Apple-Music-Link.")
    source, kind, ident = detected
    if source == "spotify":
        return fetch_spotify(kind, ident)
    return fetch_apple(url)


# --- Spotify (client-credentials Web API) ----------------------------------

_token_cache: dict = {"token": None, "expires": 0.0}
_token_lock = threading.Lock()


def _spotify_token() -> str:
    if not settings.spotify_configured:
        raise SpotifyError("Spotify ist auf dem Server nicht konfiguriert.")
    with _token_lock:
        if _token_cache["token"] and time.monotonic() < _token_cache["expires"]:
            return _token_cache["token"]
        auth = base64.b64encode(
            f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()).decode()
        try:
            resp = httpx.post("https://accounts.spotify.com/api/token",
                              data={"grant_type": "client_credentials"},
                              headers={"Authorization": f"Basic {auth}"},
                              timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - fail soft, no stack
            raise SpotifyError(f"Spotify-Login fehlgeschlagen: {str(exc)[:120]}") from exc
        _token_cache["token"] = payload["access_token"]
        _token_cache["expires"] = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
        return _token_cache["token"]


def _is_spotify_api(url: str) -> bool:
    """True only for an https://api.spotify.com/… URL (guards the token-bearing pagination)."""
    try:
        p = urlparse(url or "")
    except Exception:  # noqa: BLE001
        return False
    return p.scheme == "https" and (p.hostname or "").lower() == "api.spotify.com"


def _spotify_get(url: str, headers: dict) -> dict:
    try:
        resp = httpx.get(url, headers=headers, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - fail soft
        raise SpotifyError(f"Spotify-Abruf fehlgeschlagen: {str(exc)[:120]}") from exc


def _spotify_track(item: dict, fallback_album: Optional[str]) -> Optional[ImportedTrack]:
    # Playlist rows wrap the track in `track`; album rows are the track directly.
    t = item.get("track", item) if "track" in item else item
    if not isinstance(t, dict) or t.get("is_local") or not t.get("name"):
        return None
    artists = ", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name"))
    album = (t.get("album") or {}).get("name") or fallback_album
    return ImportedTrack(artist=artists, title=str(t["name"]), album=album)


def fetch_spotify(kind: str, ident: str) -> ImportedPlaylist:
    headers = {"Authorization": f"Bearer {_spotify_token()}"}
    base = "https://api.spotify.com/v1"
    if kind == "album":
        data = _spotify_get(f"{base}/albums/{ident}", headers)
        name = data.get("name", "Album")
        page = data.get("tracks", {})
        fallback = name
    else:
        data = _spotify_get(f"{base}/playlists/{ident}", headers)
        name = data.get("name", "Playlist")
        page = data.get("tracks", {})
        fallback = None

    tracks: list[ImportedTrack] = []
    while True:
        for item in page.get("items", []):
            tr = _spotify_track(item, fallback)
            if tr is not None:
                tracks.append(tr)
        nxt = page.get("next")
        # Only follow a `next` that stays on api.spotify.com — the URL comes from the response
        # body, and we send the access token with it; never leak the token to another host.
        if not nxt or not _is_spotify_api(nxt):
            break
        page = _spotify_get(nxt, headers)
    return ImportedPlaylist(name=name, source="spotify", source_id=ident, tracks=tracks)


# --- Apple Music (public-page JSON — fragile, best-effort) -----------------

_APPLE_DATA = re.compile(
    r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', re.DOTALL)


def _apple_collect_tracks(node: object, out: list[ImportedTrack]) -> None:
    """Walk the deserialized page data and collect song-shaped dicts (defensive, shape-tolerant)."""
    if isinstance(node, dict):
        kind = node.get("@type") or node.get("kind") or node.get("contentDescriptor", {})
        title = node.get("title") or node.get("name")
        artist = node.get("artistName") or node.get("subtitle")
        if title and artist and (kind == "song" or node.get("audioTraits") is not None
                                 or "artistName" in node):
            album = node.get("collectionName")
            out.append(ImportedTrack(artist=str(artist), title=str(title),
                                     album=album if isinstance(album, str) else None))
            return
        for v in node.values():
            _apple_collect_tracks(v, out)
    elif isinstance(node, list):
        for v in node:
            _apple_collect_tracks(v, out)


def fetch_apple(url: str) -> ImportedPlaylist:
    try:
        resp = httpx.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:  # noqa: BLE001 - fail soft
        raise AppleParseError(f"Apple-Music-Seite nicht erreichbar: {str(exc)[:120]}") from exc

    m = _APPLE_DATA.search(html)
    if not m:
        raise AppleParseError("Apple-Music-Trackliste nicht gefunden (Seitenformat geändert?).")
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError) as exc:
        raise AppleParseError(f"Apple-Music-Daten unlesbar: {str(exc)[:120]}") from exc

    tracks: list[ImportedTrack] = []
    _apple_collect_tracks(data, tracks)
    if not tracks:
        raise AppleParseError("Apple-Music-Trackliste leer oder Format geändert.")

    # De-dup exact repeats a recursive walk can pick up, preserving order.
    seen: set = set()
    unique: list[ImportedTrack] = []
    for tr in tracks:
        key = (tr.artist, tr.title)
        if key not in seen:
            seen.add(key)
            unique.append(tr)

    name = _apple_playlist_name(html) or "Apple-Music-Playlist"
    detected = detect_import_url(url)   # already validated by fetch_playlist; fall back defensively
    ident = detected[2] if detected else "apple"
    return ImportedPlaylist(name=name, source="apple", source_id=ident, tracks=unique)


def _apple_playlist_name(html: str) -> Optional[str]:
    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    # Apple titles look like "Playlist Name – Apple Music" / "… on Apple Music".
    title = re.sub(r"\s*[–—|-]\s*Apple Music.*$", "", m.group(1).strip(), flags=re.IGNORECASE)
    return title.strip() or None
