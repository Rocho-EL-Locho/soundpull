"""Library-wide duplicate finder & cleanup (roadmap 04).

Everything download/staging-time dedup can't see: duplicates ALREADY sitting in the WebDAV
library. The `ServerTrack` index can't even represent them — its unique
`(user_id, artist_norm, title_norm)` constraint collapses collisions to one row — so
detection has to happen during a **walk** of the library, not from the table.

Pipeline:
- `analyze(user_id)` walks the library (`library_index.iter_library_files`), groups files by
  `library_index.track_key` into an **exact** tier (same key at ≥2 paths) and, over the
  remaining singles, a **probable** tier (same key after `pipeline._strip_title_noise`). Each
  group gets a pre-selected keeper (biggest real-album folder wins). The result is persisted as
  JSON in `DuplicateReport` (one row per user, replaced on re-run).
- `resolve_group(user_id, keeper_rel, remove_rels)` trashes the non-keepers via
  `library_ops.trash_track` (safe trash, never hard delete), points the surviving index row at
  the keeper, and repairs any playlist `.m3u8` / subscription manifest that referenced a removed
  copy so the playlist keeps resolving in Navidrome (issue #31 cross-folder references).

This module contains NO pipeline/tagging code — metadata parity holds by construction. It reuses
`pipeline._strip_title_noise` (imported, not forked) and the same `posixpath.relpath` frame the
pipeline uses to build cross-folder m3u references.
"""
from __future__ import annotations

import json
import logging
import posixpath
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlmodel import select

from app import library_index, library_ops
from app.library_index import _artist_title_from_path, _norm, track_key
from app.pipeline import _strip_title_noise

log = logging.getLogger("duplicates")


# --- Background analysis registry ------------------------------------------
#
# A full library walk takes minutes, so the analysis runs off-thread and the /duplicates page
# polls this in-memory state via `ui.timer` (the `jobs.py` + index-page pattern). It is a
# maintenance task — no `DownloadHistory` row — with a per-user guard so a user can't start two
# concurrent walks (redundant WebDAV traffic). The durable result lives in `DuplicateReport`.

@dataclass
class AnalysisState:
    phase: str = "queued"        # queued | scanning | grouping | done | error (i18n keys on page)
    error: Optional[str] = None
    exact_count: int = 0
    probable_count: int = 0
    finished: bool = False


_analysis: dict[int, AnalysisState] = {}
_analysis_lock = threading.Lock()
_analysis_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dup-analyze")


def get_analysis_state(user_id: int) -> Optional[AnalysisState]:
    """Current analysis state for a user (None if never started this process)."""
    with _analysis_lock:
        return _analysis.get(user_id)


def is_analysis_running(user_id: int) -> bool:
    with _analysis_lock:
        st = _analysis.get(user_id)
        return st is not None and not st.finished


def start_analysis(user_id: int) -> bool:
    """Kick off a background analysis for a user. False if one is already running."""
    with _analysis_lock:
        st = _analysis.get(user_id)
        if st is not None and not st.finished:
            return False
        _analysis[user_id] = AnalysisState(phase="queued")

    def _set(**kw) -> None:
        with _analysis_lock:
            st = _analysis.get(user_id)
            if st is not None:
                for k, v in kw.items():
                    setattr(st, k, v)

    def _run() -> None:
        try:
            report = analyze(user_id, progress=lambda phase: _set(phase=phase))
            _set(phase="done", finished=True, exact_count=len(report.exact),
                 probable_count=len(report.probable))
        except Exception as exc:  # noqa: BLE001 - a failed analysis must not kill the worker
            log.exception("duplicate analysis for user %s failed", user_id)
            _set(phase="error", finished=True, error=str(exc))

    _analysis_executor.submit(_run)
    return True


# --- Data model ------------------------------------------------------------

@dataclass
class PathInfo:
    """One copy of a track in a duplicate group."""
    rel_path: str
    folder: str                 # the file's parent folder (relative to webdav_folder)
    folder_track_count: int     # audio files directly in that folder
    is_playlist_folder: bool    # a `<name> [<id>]` playlist folder (roadmap 02/issue #39)


@dataclass
class Group:
    """A set of ≥2 library files judged to be the same track."""
    tier: str                       # "exact" | "probable"
    artist: str                     # display artist (from the keeper's path)
    title: str                      # display title (from the keeper's path)
    paths: list[PathInfo]
    suggested_keeper: str           # rel_path pre-selected to keep (never auto-applied)


@dataclass
class Report:
    created_at: str                 # ISO-8601 UTC
    exact: list[Group] = field(default_factory=list)
    probable: list[Group] = field(default_factory=list)


# --- Grouping / keeper heuristic -------------------------------------------

def _is_playlist_folder(folder: str) -> bool:
    """True for a `<name> [<id>]` playlist folder — its basename ends in ``[...]``.

    Mirrors the `pipeline._playlist_folder_name` convention without importing its regex; a real
    album folder is ``Artist/Album`` (no bracketed id), a playlist folder is ``Chill [PLxxxx]``.
    """
    base = posixpath.basename(folder.rstrip("/"))
    return base.endswith("]") and " [" in base


def _keeper(paths: list[PathInfo]) -> str:
    """Pre-select which copy to keep: biggest real-album folder wins (roadmap 04 / issue #56).

    Same rationale as `pipeline._dedup_staged_tracks`: a real release beats a 1-track single,
    an artist-tree album beats a playlist folder; ties break on the shorter, then lexicographically
    smaller, path. Returns the chosen ``rel_path``.
    """
    best = max(paths, key=lambda p: (not p.is_playlist_folder, p.folder_track_count,
                                     -len(p.rel_path), _neg_lex(p.rel_path)))
    return best.rel_path


def _neg_lex(s: str) -> list[int]:
    """Sort key that makes a lexicographically SMALLER string compare as LARGER (for `max`)."""
    return [-ord(c) for c in s]


def analyze(user_id: int, progress: Optional[Callable[[str], None]] = None) -> Report:
    """Walk the user's WebDAV library and build the exact + probable duplicate report.

    `progress(message)` — optional callback for live UI progress. Persists the report into
    `DuplicateReport` (replacing any prior row) and returns it. Raises on a WebDAV
    connection/config error (an unreachable root), like `library_index.scan_webdav`; an
    incomplete walk (some sub-dir listings failed) still returns whatever was found.
    """
    from app.db import session_scope
    from app.models import UserSettings
    from app.security import decrypt_secret
    from app.webdav_util import make_client

    with session_scope() as session:
        us = session.exec(select(UserSettings).where(UserSettings.user_id == user_id)).first()
        if not us or not us.webdav_url:
            raise ValueError("Kein WebDAV-Ziel im Profil hinterlegt.")
        url, username = us.webdav_url, us.webdav_username
        password = decrypt_secret(us.webdav_password_enc) if us.webdav_password_enc else None
        base = (us.webdav_folder or "").strip("/")

    client = make_client(url, username, password)
    if progress:
        progress("scanning")

    # One pass: collect every audio file with its (key, folder) and per-folder counts.
    files: list[tuple[tuple[str, str], str, str, str]] = []  # (key, rel, artist, title)
    folder_counts: dict[str, int] = {}
    errors: list = []
    for rel in library_index.iter_library_files(client, base, errors=errors):
        parts = [p for p in rel.split("/") if p]
        if not parts:
            continue
        folder = posixpath.dirname(rel)
        folder_counts[folder] = folder_counts.get(folder, 0) + 1
        artist, title = _artist_title_from_path(parts)
        if not title:
            continue
        files.append((track_key(title, artist), rel, artist, title))

    if errors:
        log.warning("duplicate analysis: %d directory listing(s) failed — report may be partial",
                    len(errors))
    if progress:
        progress("grouping")

    report = _build_report(files, folder_counts)
    report_id = _persist(user_id, report)
    log.info("duplicate analysis for user %s: %d exact, %d probable groups (report %s)",
             user_id, len(report.exact), len(report.probable), report_id)
    return report


def _path_info(rel: str, folder_counts: dict[str, int]) -> PathInfo:
    folder = posixpath.dirname(rel)
    return PathInfo(rel_path=rel, folder=folder,
                    folder_track_count=folder_counts.get(folder, 1),
                    is_playlist_folder=_is_playlist_folder(folder))


def _build_report(files: list[tuple[tuple[str, str], str, str, str]],
                  folder_counts: dict[str, int]) -> Report:
    """Pure grouping over collected files — split out so it is unit-testable without WebDAV."""
    by_key: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for key, rel, artist, title in files:
        by_key.setdefault(key, []).append((rel, artist, title))

    exact: list[Group] = []
    singles: list[tuple[tuple[str, str], str, str, str]] = []  # remaining 1-copy tracks
    for key, copies in by_key.items():
        if len(copies) >= 2:
            exact.append(_make_group("exact", copies, folder_counts))
        else:
            rel, artist, title = copies[0]
            singles.append((key, rel, artist, title))

    # Probable tier: over the singles only, collapse titles that differ only by release noise.
    by_noise: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for key, rel, artist, title in singles:
        noise_key = (key[0], _norm(_strip_title_noise(title)))
        by_noise.setdefault(noise_key, []).append((rel, artist, title))
    probable: list[Group] = []
    for noise_key, copies in by_noise.items():
        # More than one DISTINCT exact key collapsing to the same noise key = a probable dup
        # (identical exact keys would already be in the exact tier).
        if len(copies) >= 2:
            probable.append(_make_group("probable", copies, folder_counts))

    exact.sort(key=lambda g: (g.artist.casefold(), g.title.casefold()))
    probable.sort(key=lambda g: (g.artist.casefold(), g.title.casefold()))
    return Report(created_at=datetime.now(timezone.utc).isoformat(), exact=exact,
                  probable=probable)


def _make_group(tier: str, copies: list[tuple[str, str, str]],
                folder_counts: dict[str, int]) -> Group:
    infos = [_path_info(rel, folder_counts) for rel, _, _ in copies]
    keeper = _keeper(infos)
    # Display artist/title come from the keeper's copy.
    disp = next((a, t) for rel, a, t in copies if rel == keeper)
    return Group(tier=tier, artist=disp[0], title=disp[1], paths=infos, suggested_keeper=keeper)


# --- Persistence -----------------------------------------------------------

def _persist(user_id: int, report: Report) -> int:
    from app.db import session_scope
    from app.models import DuplicateReport

    payload = json.dumps({"exact": [_group_json(g) for g in report.exact],
                          "probable": [_group_json(g) for g in report.probable]})
    with session_scope() as session:
        row = session.exec(
            select(DuplicateReport).where(DuplicateReport.user_id == user_id)).first()
        if row is None:
            row = DuplicateReport(user_id=user_id)
        row.groups = payload
        row.created_at = datetime.now(timezone.utc)
        session.add(row)
        session.flush()
        return row.id


def _group_json(g: Group) -> dict:
    return {"tier": g.tier, "artist": g.artist, "title": g.title,
            "suggested_keeper": g.suggested_keeper,
            "paths": [asdict(p) for p in g.paths]}


def load_report(user_id: int) -> Optional[Report]:
    """Load the persisted report for a user (or None if never analysed)."""
    from app.db import session_scope
    from app.models import DuplicateReport

    with session_scope() as session:
        row = session.exec(
            select(DuplicateReport).where(DuplicateReport.user_id == user_id)).first()
        if row is None:
            return None
        data = json.loads(row.groups or "{}")
        if not isinstance(data, dict):
            data = {}
        created = row.created_at
    def _groups(items: list) -> list[Group]:
        return [Group(tier=d["tier"], artist=d["artist"], title=d["title"],
                      suggested_keeper=d["suggested_keeper"],
                      paths=[PathInfo(**p) for p in d["paths"]]) for d in items]
    created_iso = created.isoformat() if isinstance(created, datetime) else str(created)
    return Report(created_at=created_iso, exact=_groups(data.get("exact", [])),
                  probable=_groups(data.get("probable", [])))


# --- Cleanup + playlist-reference repair -----------------------------------

@dataclass
class ResolveResult:
    trashed: list[str] = field(default_factory=list)      # rel_paths moved to trash
    m3u_repaired: list[str] = field(default_factory=list)  # rel_paths of rewritten .m3u8 files
    manifests_repaired: int = 0                            # subscription manifests updated


def _repoint(name: str, folder_rel: str, removed: set[str], keeper_rel: str) -> Optional[str]:
    """If an m3u line / manifest entry `name` resolves to a removed copy, return the new name.

    `name` is relative to the playlist folder (`folder_rel`) — either a bare filename (in-folder)
    or a cross-folder relative path (issue #31). Resolve it against the folder; if it points at a
    trashed copy, re-point it at the keeper using the SAME `posixpath.relpath` frame the pipeline
    uses (`pipeline.py` run_download) — a bare filename when the keeper is in the same folder,
    else a ``../Artist/Album/x.mp3`` relative path. Returns None when the line is untouched.
    """
    joined = posixpath.join(folder_rel, name) if folder_rel else name
    resolved = posixpath.normpath(joined)
    if resolved not in removed:
        return None
    return posixpath.relpath(keeper_rel, folder_rel) if folder_rel else keeper_rel


def rewrite_m3u(text: str, folder_rel: str, removed: set[str],
                keeper_rel: str) -> Optional[str]:
    """Rewrite `.m3u8` path lines pointing at a removed copy to point at the keeper.

    Pure over strings (trivially testable). Path lines are the non-blank, non-``#`` lines; each
    is resolved against `folder_rel` and re-pointed via `_repoint`. Comments/`#EXTINF` and
    untouched lines are preserved verbatim. Preserves the pipeline's m3u format: LF newlines with
    a trailing newline (`app.pipeline._write_m3u`). Returns None when NOTHING changed, so the
    caller skips the upload (issue #31 no-op case).
    """
    lines = text.split("\n")
    changed = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        new_name = _repoint(stripped, folder_rel, removed, keeper_rel)
        if new_name is not None and new_name != stripped:
            out.append(new_name)
            changed = True
        else:
            out.append(line)
    if not changed:
        return None
    # Preserve the pipeline's m3u format: LF-joined with a single trailing newline (_write_m3u).
    body = "\n".join(out)
    return body if body.endswith("\n") else body + "\n"


def _rewrite_manifest(entries: list[dict], folder_rel: str, removed: set[str],
                      keeper_rel: str) -> Optional[list[dict]]:
    """Re-point a subscription's playlist manifest entries (JSON) the same way as `rewrite_m3u`.

    Each entry's ``name`` is relative to the playlist folder. Returns a new entry list when any
    entry was re-pointed, else None (no DB write needed).
    """
    changed = False
    out: list[dict] = []
    for entry in entries:
        name = entry.get("name", "")
        new_name = _repoint(name, folder_rel, removed, keeper_rel)
        if new_name is not None and new_name != name:
            out.append({**entry, "name": new_name})
            changed = True
        else:
            out.append(entry)
    return out if changed else None


def repair_playlist_refs(user_id: int, removed: set[str], keeper_rel: str) -> ResolveResult:
    """Rewrite every playlist `.m3u8` and subscription manifest that referenced a removed copy.

    Best-effort (like the `jobs.py` `_*_safe` side effects): a per-file failure is logged and
    swallowed, never aborting the cleanup. Returns what was repaired.
    """
    result = ResolveResult()
    if not removed:
        return result

    # 1) On-disk .m3u8 files (what Navidrome actually reads).
    try:
        m3u_rels = library_ops.list_playlist_files(user_id)
    except Exception as exc:  # noqa: BLE001 - repair is best-effort
        log.warning("duplicate repair: listing playlist files failed: %s", exc)
        m3u_rels = []
    for rel in m3u_rels:
        folder = posixpath.dirname(rel)
        try:
            text = library_ops.read_library_text(user_id, rel)
            new = rewrite_m3u(text, folder, removed, keeper_rel)
            if new is not None:
                library_ops.write_library_text(user_id, rel, new)
                result.m3u_repaired.append(rel)
        except Exception as exc:  # noqa: BLE001
            log.warning("duplicate repair: rewriting %r failed: %s", rel, exc)

    # 2) Subscription manifests (so a later sync doesn't regenerate the stale reference).
    result.manifests_repaired = _repair_subscription_manifests(user_id, m3u_rels, removed,
                                                                keeper_rel)
    return result


def _subscription_folder(sub_name: str, m3u_rels: list[str]) -> Optional[str]:
    """Best-effort map a subscription to its playlist folder via the on-disk `.m3u8` name.

    A subscription's playlist is delivered to ``<name> [<id>]/<name>.m3u8`` where ``<name>`` is
    `pipeline._safe_segment(sub.name)`. Match the m3u whose basename stem equals that safe name;
    return its folder. Ambiguous (≥2 matches) or missing → None (that subscription is skipped).
    """
    from app.pipeline import _safe_segment

    stem = _safe_segment(sub_name)
    matches = [posixpath.dirname(rel) for rel in m3u_rels
               if posixpath.splitext(posixpath.basename(rel))[0] == stem]
    return matches[0] if len(matches) == 1 else None


def _repair_subscription_manifests(user_id: int, m3u_rels: list[str], removed: set[str],
                                   keeper_rel: str) -> int:
    from app.db import session_scope
    from app.models import PlaylistSubscription

    repaired = 0
    try:
        with session_scope() as session:
            subs = session.exec(
                select(PlaylistSubscription).where(
                    PlaylistSubscription.user_id == user_id,
                    PlaylistSubscription.playlist_files.is_not(None))).all()
            for sub in subs:
                folder = _subscription_folder(sub.name, m3u_rels)
                if folder is None:
                    continue
                try:
                    entries = json.loads(sub.playlist_files or "[]")
                except (ValueError, TypeError):
                    continue
                new_entries = _rewrite_manifest(entries, folder, removed, keeper_rel)
                if new_entries is not None:
                    sub.playlist_files = json.dumps(new_entries)
                    session.add(sub)
                    repaired += 1
    except Exception as exc:  # noqa: BLE001 - best-effort
        log.warning("duplicate repair: subscription manifest update failed: %s", exc)
    return repaired


def resolve_group(user_id: int, keeper_rel: str, remove_rels: list[str]) -> ResolveResult:
    """Trash the non-keeper copies, fix the index, and repair playlist references (roadmap 04).

    1. Trash each `remove_rel` via `library_ops.trash_track` (safe trash; nothing hard-deleted).
    2. Ensure the surviving index row for this track points at `keeper_rel` (trashing a copy that
       happened to be the indexed one drops the row — re-record the keeper so a re-scan doesn't
       resurrect the duplicate).
    3. Repair any `.m3u8` / subscription manifest that referenced a removed copy.
    """
    from app.db import session_scope

    result = ResolveResult()
    removed: set[str] = set()
    for rel in remove_rels:
        if rel == keeper_rel:
            continue
        try:
            library_ops.trash_track(user_id, rel)
            result.trashed.append(rel)
            removed.add(rel)
        except Exception as exc:  # noqa: BLE001 - surface via the caller; keep going
            log.warning("duplicate resolve: trashing %r failed: %s", rel, exc)

    # Ensure the key row points at the keeper (re-inserts it if a trashed copy was the indexed one).
    parts = [p for p in keeper_rel.split("/") if p]
    artist, title = _artist_title_from_path(parts)
    if title:
        with session_scope() as session:
            library_index.record_tracks(session, user_id, [(artist, title, keeper_rel)],
                                        update_path=True)

    repair = repair_playlist_refs(user_id, removed, keeper_rel)
    result.m3u_repaired = repair.m3u_repaired
    result.manifests_repaired = repair.manifests_repaired
    return result
