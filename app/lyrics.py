"""Synced-lyrics fetching (issue #43).

Best-effort, non-fatal fetch of synced lyrics from LRCLIB (https://lrclib.net) written
as `.lrc` sidecar files next to each downloaded track. Navidrome imports `.lrc` sidecars
natively (like it does the `.m3u8` playlists), so this is a purely ADDITIVE step: it never
touches the frozen tag-write path in `fix_music_tags.py`, so metadata parity is preserved
by construction. Any miss/error just skips — a track without lyrics simply gets no sidecar
and the job still succeeds (mirrors the cover fetch in `app/pipeline.py`).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

import httpx

log = logging.getLogger("lyrics")

_LRCLIB_BASE = "https://lrclib.net"
_TIMEOUT = 10
# Bounded concurrency for bulk sidecar writes: fast for a large playlist without
# hammering the community LRCLIB service into rate-limiting us.
_MAX_WORKERS = 8

try:  # version is cosmetic — only used to identify ourselves to LRCLIB
    from importlib.metadata import version

    _VERSION = version("soundpull")
except Exception:  # noqa: BLE001
    _VERSION = "0.0.0"

# LRCLIB asks clients to send a descriptive User-Agent identifying the app.
_USER_AGENT = f"soundpull/{_VERSION} (+https://github.com/Rocho-EL-Locho/soundpull)"


class _TransientLookupError(Exception):
    """A retryable LRCLIB failure (network error, timeout, HTTP 429, or 5xx).

    Raised out of the cached lookup so the miss is NOT memoized — a temporary blip
    must not permanently blacklist a track for the whole process lifetime; a later
    call (e.g. a re-download or an interval sync) then retries instead.
    """


def _pick_lyrics(entry: object) -> str | None:
    """Prefer synced lyrics; fall back to plain; skip instrumentals/empties.

    Tolerant of a non-dict entry (a malformed response) so bulk fetching never raises.
    """
    if not isinstance(entry, dict) or entry.get("instrumental"):
        return None
    synced = (entry.get("syncedLyrics") or "").strip()
    if synced:
        return synced
    plain = (entry.get("plainLyrics") or "").strip()
    return plain or None


def _get(path: str, params: dict) -> object | None:
    """GET an LRCLIB endpoint.

    Returns the parsed JSON on 200, None on a DEFINITIVE non-200 (e.g. 404 → no such
    track), and raises `_TransientLookupError` on a retryable failure (network error,
    timeout, HTTP 429, or 5xx) so the caller does not cache it.
    """
    try:
        resp = httpx.get(f"{_LRCLIB_BASE}{path}", params=params,
                         headers={"User-Agent": _USER_AGENT},
                         timeout=_TIMEOUT, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - a network/timeout error is transient
        raise _TransientLookupError(str(exc)) from exc
    if resp.status_code == 200:
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - a malformed 200 is worth a retry
            raise _TransientLookupError(f"bad json: {exc}") from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        raise _TransientLookupError(f"HTTP {resp.status_code}")
    return None  # other 4xx (typically 404) → definitive: no lyrics for this query


@lru_cache(maxsize=4096)
def _cached_lookup(artist: str, title: str, album: str | None,
                   duration: int | None) -> str | None:
    """Definitive lyrics lookup, memoized per (artist, title, album, duration).

    Returns the lyrics text or None (a real "no lyrics") — both are definitive and get
    cached. Raises `_TransientLookupError` on a retryable failure, which lru_cache does
    NOT memoize, so the blip can be retried later.
    """
    # 1) Exact match via /api/get when we know the duration (LRCLIB matches within ±2s).
    if duration and duration > 0:
        params = {"artist_name": artist, "track_name": title, "duration": duration}
        if album:
            params["album_name"] = album
        data = _get("/api/get", params)
        if data is not None:
            text = _pick_lyrics(data)
            if text:
                return text
        # 404 or a 200 without usable lyrics → fall through to a fuzzy search.
    # 2) Fallback: fuzzy search, take the first result that carries lyrics.
    results = _get("/api/search", {"track_name": title, "artist_name": artist})
    for entry in results if isinstance(results, list) else []:
        text = _pick_lyrics(entry)
        if text:
            return text
    return None  # definitive miss — safe to cache


def fetch_synced_lyrics(artist: str, title: str, album: str | None = None,
                        duration: int | None = None) -> str | None:
    """Fetch synced (or plain) lyrics for a track from LRCLIB; None if unavailable.

    Best-effort and never raises. Definitive results (a hit or a real "no lyrics") are
    cached per (artist, title, album, duration); TRANSIENT failures (network/timeout/
    429/5xx) are NOT cached, so a later call retries instead of being stuck on a blip.
    """
    if not title or not artist:
        return None
    try:
        return _cached_lookup(artist, title, album, duration)
    except _TransientLookupError as exc:
        log.warning("lyrics lookup transient failure for %s - %s: %s", artist, title, exc)
        return None


# Expose the cache controls on the public entry point (used by callers/tests).
fetch_synced_lyrics.cache_clear = _cached_lookup.cache_clear  # type: ignore[attr-defined]
fetch_synced_lyrics.cache_info = _cached_lookup.cache_info    # type: ignore[attr-defined]


def _read_query(audio_path: Path) -> tuple[str, str, str | None, int | None] | None:
    """(artist, title, album, duration) from a track's FINAL tags, or None.

    Uses the PRIMARY artist (first ` / ` segment) — playlist per-track tags hold
    `Primary / Feat` and LRCLIB matches the primary artist better.
    """
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(audio_path), easy=True)
        if audio is None:
            return None
        title = (audio.get("title") or [""])[0].strip()
        artist = (audio.get("artist") or [""])[0].split(" / ")[0].strip()
        album = ((audio.get("album") or [""])[0]).strip() or None
        dur = int(getattr(audio.info, "length", 0) or 0)
        if not title or not artist:
            return None
        return artist, title, album, (dur or None)
    except Exception:  # noqa: BLE001 - never fail the job over lyrics
        return None


def write_lrc_for(audio_path: Path) -> bool:
    """Fetch lyrics for `audio_path` and write a `<stem>.lrc` sidecar. Best-effort.

    Returns True iff a sidecar was written. Never raises.
    """
    query = _read_query(audio_path)
    if query is None:
        return False
    text = fetch_synced_lyrics(*query)
    if not text:
        return False
    try:
        audio_path.with_suffix(".lrc").write_text(text, encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001 - never fail the job over lyrics
        log.warning("lrc write failed for %s: %s", audio_path.name, exc)
        return False


def write_lrc_sidecars(audio_paths: Iterable[Path],
                       progress: Callable[[int, int], None] | None = None,
                       max_workers: int = _MAX_WORKERS) -> int:
    """Fetch + write `.lrc` sidecars for many tracks CONCURRENTLY (best-effort).

    Runs `write_lrc_for` on a bounded thread pool so a large playlist doesn't serialise
    N blocking HTTP round-trips. Reports `progress(done, total)` from the calling thread
    as each track finishes (0/total up front). Returns the number of sidecars written.
    Never raises — `write_lrc_for` swallows every per-track error.
    """
    paths = list(audio_paths)
    total = len(paths)
    if not total:
        return 0
    if progress:
        progress(0, total)
    written = done = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, total),
                            thread_name_prefix="lyrics") as pool:
        futures = [pool.submit(write_lrc_for, p) for p in paths]
        for fut in as_completed(futures):
            if fut.result():
                written += 1
            done += 1
            if progress:
                progress(done, total)
    return written
