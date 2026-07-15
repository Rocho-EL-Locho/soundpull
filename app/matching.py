"""Track-list parsing + YouTube-Music matching (roadmap 12 batch import).

Turns a pasted list (``Artist - Title`` per line, or a simple CSV) into `Match` objects: each
input line is matched against YouTube Music (`app.search.search_songs`), scored by similarity, and
flagged if it's already in the user's library. This is a **pure/UI-free** module — feature 13
(Spotify/Apple import) consumes `match_all` directly without the page.

Normalization is shared with the library index (`library_index._norm` / `_clean_title` /
`_primary_artist` / `track_key`) so "already in library" and the confidence score agree on what
"equal" means. Scoring is `difflib.SequenceMatcher` over those normalized strings, combined as
``min(artist_ratio, title_ratio)`` — both the artist AND the title must match, so an exact title by
the wrong artist never passes. No pipeline/tag code is touched.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable, Optional

from app import search
from app.library_index import _clean_title, _norm, _primary_artist, track_key
from app.search import SearchResult

log = logging.getLogger("matching")

MAX_LINES = 200                 # hard cap on a pasted list (roadmap 12 scope)
_CANDIDATES = 3                 # top-N alternatives kept per line for the review dropdown
HIGH_CONFIDENCE = 0.85          # ≥ → pre-checked in the review UI
_MATCH_WORKERS = 3              # bounded pool — fast enough, doesn't hammer ytmusicapi

# En dash / em dash / hyphen, each surrounded by spaces — the accepted "Artist - Title" separators.
_SEPARATORS = (" - ", " – ", " — ")


@dataclass(frozen=True)
class ParsedLine:
    raw: str
    artist: Optional[str] = None
    title: Optional[str] = None
    error: Optional[str] = None    # i18n key when the line couldn't be parsed

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.artist) and bool(self.title)


@dataclass
class Match:
    line: ParsedLine
    candidates: list[SearchResult] = field(default_factory=list)  # top-N songs, best first
    best: Optional[SearchResult] = None
    confidence: float = 0.0
    on_server: bool = False


# --- parsing ----------------------------------------------------------------

def _split_pair(text: str) -> Optional[tuple[str, str]]:
    """Split ``Artist <sep> Title`` on the LEFTMOST separator (a title may contain ' - ').

    "First separator" means earliest by position, across all accepted dash types — so a title with
    an internal ` - ` stays intact because the artist is whatever precedes the first separator.
    """
    best_pos: Optional[int] = None
    best_sep = ""
    for sep in _SEPARATORS:
        pos = text.find(sep)
        if pos != -1 and (best_pos is None or pos < best_pos):
            best_pos, best_sep = pos, sep
    if best_pos is None:
        return None
    artist = text[:best_pos].strip()
    title = text[best_pos + len(best_sep):].strip()
    return (artist, title) if artist and title else None


def _looks_like_csv_header(line: str) -> bool:
    cells = [c.strip().lower() for c in line.split(",")]
    return cells[:2] == ["artist", "title"]


def parse_lines(text: str) -> list[ParsedLine]:
    """Parse a pasted list into `ParsedLine`s (capped at `MAX_LINES`).

    Accepts: ``Artist - Title`` (hyphen/en/em dash), TAB-separated, and CSV with an ``artist,title``
    header. A ``Title - Artist`` order is NOT guessed (ambiguous — the first segment is the artist by
    contract). Unparseable lines get an `error` key and are kept (never silently dropped). Blank
    lines are ignored.
    """
    rows = [ln for ln in (text or "").splitlines()]
    non_empty = [(i, ln) for i, ln in enumerate(rows) if ln.strip()]
    csv_mode = bool(non_empty) and _looks_like_csv_header(non_empty[0][1])

    out: list[ParsedLine] = []
    header_index = non_empty[0][0] if csv_mode else -1
    for i, ln in enumerate(rows):
        if not ln.strip():
            continue
        if i == header_index:
            continue  # skip the CSV header row
        raw = ln.strip()
        pair: Optional[tuple[str, str]] = None
        if csv_mode:
            try:
                cells = next(csv.reader([ln]))
            except Exception:  # noqa: BLE001
                cells = []
            if len(cells) >= 2 and cells[0].strip() and cells[1].strip():
                pair = (cells[0].strip(), cells[1].strip())
        elif "\t" in ln:
            a, _, t = ln.partition("\t")
            if a.strip() and t.strip():
                pair = (a.strip(), t.strip())
        if pair is None:
            pair = _split_pair(raw)
        if pair is None:
            out.append(ParsedLine(raw=raw, error="import.parse_error"))
        else:
            out.append(ParsedLine(raw=raw, artist=pair[0], title=pair[1]))
        if len(out) >= MAX_LINES:
            break
    return out


# --- scoring ----------------------------------------------------------------

def _ratio(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def score(parsed: ParsedLine, result: SearchResult) -> float:
    """Confidence 0..1 that `result` is `parsed` — ``min`` of artist & title similarity.

    Uses the SAME normalization as `track_key` (`_primary_artist`/`_clean_title` + `_norm`), so a
    high score and an "already in library" hit agree. Taking the min means both must match: an exact
    title credited to the wrong artist scores low.
    """
    if not parsed.ok:
        return 0.0
    artist_r = _ratio(_primary_artist(parsed.artist or ""), _primary_artist(result.artist or ""))
    title_r = _ratio(_clean_title(parsed.title or ""), _clean_title(result.title or ""))
    return min(artist_r, title_r)


# --- matching ---------------------------------------------------------------

def match_all(user_id: int, lines: list[ParsedLine],
              progress: Optional[Callable[[int, int], None]] = None) -> list[Match]:
    """Match each parsed line against YouTube Music (bounded concurrency, progress-reported).

    Parseable lines are searched (songs only) and scored; the top-`_CANDIDATES` become the review
    alternatives, the best its pre-selection. `on_server` marks a track already in the user's library
    (`track_key` membership in `load_index_paths`). Unparseable lines pass through as empty `Match`es
    so the UI can report them. Search errors on a single line degrade that line to unmatched — the
    batch never aborts. `progress(done, total)` fires from the pool as each line finishes.
    """
    from app.db import session_scope
    from app.library_index import load_index_paths

    with session_scope() as session:
        on_server_keys = set(load_index_paths(session, user_id).keys())

    total = len(lines)
    if progress:
        progress(0, total)

    def _match_one(parsed: ParsedLine) -> Match:
        if not parsed.ok:
            return Match(line=parsed)
        query = f"{parsed.artist} {parsed.title}"
        try:
            candidates = search.search_songs(query, limit=_CANDIDATES)
        except search.SearchError:
            return Match(line=parsed)          # unmatched, but not fatal
        scored = sorted(candidates, key=lambda r: score(parsed, r), reverse=True)
        best = scored[0] if scored else None
        conf = score(parsed, best) if best else 0.0
        on_server = bool(best) and track_key(best.title, best.artist) in on_server_keys
        return Match(line=parsed, candidates=scored, best=best, confidence=conf,
                     on_server=on_server)

    results: list[Optional[Match]] = [None] * total
    done = 0
    with ThreadPoolExecutor(max_workers=_MATCH_WORKERS, thread_name_prefix="match") as pool:
        futures = {pool.submit(_match_one, ln): idx for idx, ln in enumerate(lines)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception:  # noqa: BLE001 - a line's failure must not sink the batch
                log.info("match failed for line %d", idx)
                results[idx] = Match(line=lines[idx])
            done += 1
            if progress:
                progress(done, total)
    return [m if m is not None else Match(line=lines[i]) for i, m in enumerate(results)]


# --- background registry (mirrors app.health / app.duplicates) --------------

@dataclass
class MatchState:
    phase: str = "queued"          # queued | matching | done | error
    error: Optional[str] = None
    done_count: int = 0
    total_count: int = 0
    matches: list[Match] = field(default_factory=list)
    finished: bool = False


_matches: dict[int, MatchState] = {}
_match_lock = threading.Lock()
_match_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="match-run")


def get_match_state(user_id: int) -> Optional[MatchState]:
    with _match_lock:
        return _matches.get(user_id)


def is_matching(user_id: int) -> bool:
    with _match_lock:
        st = _matches.get(user_id)
        return st is not None and not st.finished


def start_match(user_id: int, text: str) -> bool:
    """Kick off background matching of a pasted list. False if one is already running."""
    with _match_lock:
        st = _matches.get(user_id)
        if st is not None and not st.finished:
            return False
        _matches[user_id] = MatchState(phase="queued")

    lines = parse_lines(text)

    def _set(**kw) -> None:
        with _match_lock:
            st = _matches.get(user_id)
            if st is not None:
                for k, v in kw.items():
                    setattr(st, k, v)

    def _run() -> None:
        try:
            _set(phase="matching", total_count=len(lines))
            matches = match_all(user_id, lines,
                                progress=lambda d, t: _set(done_count=d, total_count=t))
            _set(phase="done", finished=True, matches=matches)
        except Exception as exc:  # noqa: BLE001 - a failed match run must not kill the worker
            log.exception("batch match for user %s failed", user_id)
            _set(phase="error", finished=True, error=str(exc))

    _match_executor.submit(_run)
    return True
