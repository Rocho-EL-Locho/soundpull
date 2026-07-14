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
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

import httpx

log = logging.getLogger("lyrics")

_LRCLIB_BASE = "https://lrclib.net"
# LRCLIB is a community service and genuinely slow (single requests take several seconds,
# worse under concurrency), so give each request generous headroom — a tight timeout was
# silently dropping most tracks of a large download.
_TIMEOUT = 20
# Bounded concurrency for bulk sidecar writes: enough to parallelise a large discography
# without stampeding the slow LRCLIB service (which then times out / rate-limits us).
_MAX_WORKERS = 4
# A candidate whose length differs from ours by more than this is a different version/track
# (live/remix/extended) whose synced timestamps wouldn't line up → reject it.
_DURATION_TOLERANCE = 20

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


# --- Query normalisation & candidate matching (issue #43 accuracy) ------------
# YouTube (Music) titles/artists carry noise LRCLIB doesn't ("(Official Video)",
# a "- Topic" artist, …). We clean the query so it matches, and we score LRCLIB's
# candidates against our track so a wrong song's lyrics are never attached.

# Only strip a bracketed group that CONTAINS a noise word, so a real title like
# "Everything (I Do)" survives.
_TITLE_NOISE = re.compile(
    r"""\s*[\(\[][^\)\]]*\b(
        official|lyrics?|audio|video|visuali[sz]er|remaster(?:ed)?|
        explicit|clean|hd|hq|4k|8k|mv|m/v|radio\s*edit|sped\s*up|
        color\s*coded|full\s*album|with\s*lyrics
    )\b[^\)\]]*[\)\]]""",
    re.IGNORECASE | re.VERBOSE,
)
_TRAILING_NOISE = re.compile(
    r"\s*[-|]\s*(official\b.*|lyrics?\b.*|audio|video|visuali[sz]er)\s*$", re.IGNORECASE)


def _clean_title(title: str) -> str:
    t = _TITLE_NOISE.sub("", title or "")
    t = _TRAILING_NOISE.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—|")
    return t or (title or "")


def _clean_artist(artist: str) -> str:
    a = (artist or "").split(" / ")[0]                          # primary artist only
    a = re.sub(r"\s*-\s*topic\s*$", "", a, flags=re.IGNORECASE)  # YouTube auto channels
    a = re.sub(r"\s*vevo\s*$", "", a, flags=re.IGNORECASE)
    return a.strip() or (artist or "")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _score(cand: object, title: str, artist: str, duration: int | None) -> float:
    """Confidence (0..1) that `cand` is our track. 0 = reject.

    Both title and artist must be plausibly the same, and a candidate whose length is far
    off (a different version/track) is rejected outright — better no `.lrc` than the wrong
    song's lyrics.
    """
    if not isinstance(cand, dict):
        return 0.0
    ts = _similar(cand.get("trackName", ""), title)
    ars = _similar(cand.get("artistName", ""), artist)
    if ts < 0.5 or ars < 0.4:
        return 0.0
    cd = cand.get("duration")
    has_dur = bool(duration and duration > 0 and isinstance(cd, (int, float)) and cd > 0)
    if has_dur and abs(cd - duration) > _DURATION_TOLERANCE:
        return 0.0                              # different version/track
    score = 0.6 * ts + 0.4 * ars
    if has_dur and abs(cd - duration) <= 2:
        score = min(1.0, score + 0.1)           # exact duration → small confidence boost
    return score


def _best_match(results: object, title: str, artist: str, duration: int | None) -> dict | None:
    """The highest-scoring search candidate that carries lyrics, or None if none qualify."""
    best: dict | None = None
    best_score = 0.0
    for cand in results if isinstance(results, list) else []:
        if not isinstance(cand, dict) or not (cand.get("syncedLyrics") or cand.get("plainLyrics")):
            continue
        s = _score(cand, title, artist, duration)
        if s > best_score:
            best, best_score = cand, s
    return best


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
    ctitle, cartist = _clean_title(title), _clean_artist(artist)
    # 1) Exact version match via /api/get with the duration (LRCLIB matches within ±2s), so
    #    the synced timestamps line up with THIS audio. Album is tolerant; duration is strict.
    #    A 200 here is a confirmed title+artist+duration match, so trust it directly.
    if duration and duration > 0:
        params = {"artist_name": cartist, "track_name": ctitle, "duration": duration}
        if album:
            params["album_name"] = album
        text = _pick_lyrics(_get("/api/get", params))
        if text:
            return text
        # A duration mismatch (>2s — very common, YT masters differ) 404s here; don't give up.
    # 2) Canonical match via /api/get WITHOUT the duration — fast, but VERIFY the returned
    #    best-match really is our track (title/artist/duration score) before trusting it.
    cand = _get("/api/get", {"artist_name": cartist, "track_name": ctitle})
    if _score(cand, ctitle, cartist, duration) > 0:
        text = _pick_lyrics(cand)
        if text:
            return text
    # 3) Fuzzy search: score every candidate and take the best that clears the bar (or None).
    results = _get("/api/search", {"track_name": ctitle, "artist_name": cartist})
    return _pick_lyrics(_best_match(results, ctitle, cartist, duration))


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
