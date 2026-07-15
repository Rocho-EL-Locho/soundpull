"""In-app YouTube Music search (roadmap 07, issue #41).

A thin, import-isolated wrapper around **ytmusicapi** (unauthenticated YouTube Music InnerTube
client) that turns a query into normalized `SearchResult`s and maps each to a canonical URL the
download pipeline already accepts (`app.sources.is_supported_url`). yt-dlp's `ytsearch:` only finds
plain videos — no albums/artists — so it isn't enough for this feature.

**Isolation & resilience** (the unofficial API can break with YT changes):
- `ytmusicapi` is imported lazily inside `_client()`, so app startup and every other feature stay
  independent of the dependency.
- The client uses a `requests.Session` with a hard per-request timeout, so a slow/hung InnerTube
  call can't tie up a worker thread.
- Every public call fails soft: any underlying error becomes one `SearchError` with a short,
  stack-free message (the UI shows a warning toast and leaves the download form untouched).

No tag/pipeline code is touched here — this only produces URLs — so metadata parity is unaffected.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("search")

_MUSIC = "https://music.youtube.com"
_REQUEST_TIMEOUT = 15  # seconds; bounds every InnerTube request (ytmusicapi uses `requests`)

# Search filter → our normalized kind. `songs` gives album/artist context; the four kinds cover
# everything the download form can act on.
_FILTER_KINDS = (("songs", "song"), ("albums", "album"),
                 ("artists", "artist"), ("playlists", "playlist"))

_client_instance = None
_client_lock = threading.Lock()


class SearchError(Exception):
    """Any search/resolve failure, surfaced to the UI as a soft warning (never a stack)."""


@dataclass(frozen=True)
class SearchResult:
    kind: str                       # song | album | artist | playlist
    title: str
    artist: str                     # subtitle for artists/playlists
    url: Optional[str]              # None for an album whose audio-playlist id must be resolved
    browse_id: Optional[str]        # album MPREb_… (for on-click resolve); else None
    thumbnail: Optional[str]


def _client():
    """Lazily build one `YTMusic` instance with a timeout-bounded session (thread-safe)."""
    global _client_instance
    with _client_lock:
        if _client_instance is None:
            import requests
            from requests.adapters import HTTPAdapter
            from ytmusicapi import YTMusic

            class _TimeoutAdapter(HTTPAdapter):
                def send(self, *args, **kwargs):
                    kwargs.setdefault("timeout", _REQUEST_TIMEOUT)
                    return super().send(*args, **kwargs)

            session = requests.Session()
            session.mount("https://", _TimeoutAdapter())
            session.mount("http://", _TimeoutAdapter())
            _client_instance = YTMusic(requests_session=session)
        return _client_instance


# --- pure URL builders (unit-testable, no network) --------------------------

def song_url(video_id: str) -> str:
    return f"{_MUSIC}/watch?v={video_id}"


def artist_url(browse_id: str) -> str:
    return f"{_MUSIC}/channel/{browse_id}"


def playlist_url(list_id: str) -> str:
    # Search results give a `VL`-prefixed browseId; the `?list=` param wants the bare id.
    if list_id.startswith("VL"):
        list_id = list_id[2:]
    return f"{_MUSIC}/playlist?list={list_id}"


def album_url(audio_playlist_id: str) -> str:
    return f"{_MUSIC}/playlist?list={audio_playlist_id}"


# --- normalization ----------------------------------------------------------

def _first_artist(item: dict) -> str:
    artists = item.get("artists")
    if isinstance(artists, list) and artists:
        return str(artists[0].get("name") or "").strip()
    # albums/artists carry a plain `artist`/`author` string instead of an `artists` list
    return str(item.get("artist") or item.get("author") or "").strip()


def _thumbnail(item: dict) -> Optional[str]:
    thumbs = item.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        return thumbs[-1].get("url")  # last = largest
    return None


def _normalize(item: dict, kind: str) -> Optional[SearchResult]:
    """Map one raw ytmusicapi result to a SearchResult, or None if it's unusable."""
    if not isinstance(item, dict):
        return None
    thumb = _thumbnail(item)
    if kind == "song":
        vid = item.get("videoId")
        if not vid:
            return None
        return SearchResult("song", str(item.get("title") or "").strip(), _first_artist(item),
                            song_url(vid), None, thumb)
    if kind == "album":
        # The filtered album result usually already carries the OLAK5uy_ audio-playlist id → use it
        # directly; otherwise defer to `resolve_album_url(browse_id)` on click.
        pid = item.get("playlistId")
        browse = item.get("browseId")
        if not pid and not browse:
            return None
        return SearchResult("album", str(item.get("title") or "").strip(), _first_artist(item),
                            album_url(pid) if pid else None, browse, thumb)
    if kind == "artist":
        browse = item.get("browseId")
        if not browse:
            return None
        name = str(item.get("artist") or item.get("title") or "").strip()
        return SearchResult("artist", name, "", artist_url(browse), None, thumb)
    if kind == "playlist":
        list_id = item.get("playlistId") or item.get("browseId")
        if not list_id:
            return None
        return SearchResult("playlist", str(item.get("title") or "").strip(),
                            _first_artist(item), playlist_url(str(list_id)), None, thumb)
    return None


# --- public API -------------------------------------------------------------

def search_music(query: str, limit: int = 5) -> list[SearchResult]:
    """Search YouTube Music, returning up to `limit` results per kind.

    Resilient to the unofficial API drifting: a single malformed item is skipped and a single
    failing category is dropped, so partial breakage still returns whatever parsed. Raises
    `SearchError` only when the client can't be built or EVERY category failed (a real outage),
    so the UI can show its warning toast; otherwise it returns best-effort results.
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        yt = _client()
    except Exception as exc:  # noqa: BLE001 - client init failed → surface as a soft warning
        log.info("music search client init failed: %s", exc)
        raise SearchError(str(exc)[:200]) from exc

    results: list[SearchResult] = []
    failed = 0
    for filt, kind in _FILTER_KINDS:
        try:
            raw = yt.search(q, filter=filt, limit=limit)
        except Exception as exc:  # noqa: BLE001 - one category's failure must not sink the rest
            failed += 1
            log.info("music search (%s) for %r failed: %s", filt, q, exc)
            continue
        for item in (raw or [])[:limit]:
            try:
                r = _normalize(item, kind)
            except Exception as exc:  # noqa: BLE001 - skip a single malformed item, keep the rest
                log.info("skipping malformed %s result: %s", kind, exc)
                continue
            if r is not None:
                results.append(r)
    if failed == len(_FILTER_KINDS):  # every category errored → a real failure, surface it
        raise SearchError("music search failed")
    return results


def resolve_album_url(browse_id: str) -> str:
    """Resolve an album `browseId` (MPREb_…) to its downloadable OLAK5uy_ playlist URL (on click)."""
    try:
        yt = _client()
        album = yt.get_album(browse_id)
        pid = (album or {}).get("audioPlaylistId")
        if not pid:
            raise SearchError("album has no audio playlist id")
        return album_url(pid)
    except SearchError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail soft
        log.info("album resolve failed for %r: %s", browse_id, exc)
        raise SearchError(str(exc)[:200]) from exc
