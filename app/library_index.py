"""Per-user "what's already on the server" index (issue #21).

The detector for playlist interval-sync answers: *does `<artist>` already have
`<title>` on the server?* We keep a per-user index of normalised
`(artist_norm, title_norm)` pairs (table `ServerTrack`):

- populated on every successful **WebDAV** upload (see `app.jobs`), and
- seedable via `scan_webdav()` which walks the user's WebDAV target folder.

Normalisation is the crux: the same track can arrive as raw yt-dlp metadata
("Primary, Feat" / "Song (feat. Feat)") at match-filter time, or as the already
tagged file ("Primary / Feat" / "Song") when we record what we delivered. `track_key`
collapses both forms to the same key so a lookup matches either way. It reuses the
frozen feat-title patterns from `app.fix_music_tags` so the key equals the title we
actually write to the file.
"""
from __future__ import annotations

import logging
import os
import re

from sqlmodel import Session, select

from app import fix_music_tags

log = logging.getLogger("library_index")


def _norm(text: str) -> str:
    """Casefold + collapse whitespace so trivial differences don't split a key."""
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _primary_artist(artist: str) -> str:
    """First artist, whether separated by ' / ' (tagged) or ', ' (raw yt-dlp)."""
    return re.split(r"\s*/\s*|,\s*", (artist or "").strip(), maxsplit=1)[0].strip()


def _clean_title(title: str) -> str:
    """Strip a "(feat. …)" suffix exactly like `fix_music_tags` does when tagging."""
    out = title or ""
    for pattern in fix_music_tags.FEAT_PATTERNS:
        if re.search(pattern, out, re.IGNORECASE):
            out = re.sub(pattern, "", out, flags=re.IGNORECASE).strip()
            break
    return out


def track_key(title: str, artist: str) -> tuple[str, str]:
    """Normalised ``(artist_norm, title_norm)`` matching how a track is tagged.

    Consistent for both the raw yt-dlp form (comma-separated artists, "(feat. …)"
    in the title) and the final tagged form (" / "-separated artist, clean title),
    so a match-filter lookup and a delivered-track recording produce the same key.
    """
    return _norm(_primary_artist(artist)), _norm(_clean_title(title))


def load_index(session: Session, user_id: int) -> set[tuple[str, str]]:
    """All ``(artist_norm, title_norm)`` keys on the server for a user.

    Loaded once per sync into a set so the per-track match-filter check needs no DB
    round-trip in the hot download loop.
    """
    from app.models import ServerTrack

    rows = session.exec(
        select(ServerTrack.artist_norm, ServerTrack.title_norm)
        .where(ServerTrack.user_id == user_id)
    ).all()
    return {(a, t) for a, t in rows}


def load_index_paths(session: Session, user_id: int) -> dict[tuple[str, str], str | None]:
    """All ``(artist_norm, title_norm) -> rel_path`` for a user (issue #31).

    One query that serves both dedup needs: the skip decision is ``key in dict`` and the
    playlist m3u reference is ``dict.get(key)`` (the delivered file's library-relative path,
    or ``None`` when the track is known but its path isn't — then it's skipped, not referenced).
    """
    from app.models import ServerTrack

    rows = session.exec(
        select(ServerTrack.artist_norm, ServerTrack.title_norm, ServerTrack.rel_path)
        .where(ServerTrack.user_id == user_id)
    ).all()
    return {(a, t): p for a, t, p in rows}


def is_on_server(session: Session, user_id: int, artist: str, title: str) -> bool:
    """True if this user already has `<artist> - <title>` on the server."""
    from app.models import ServerTrack

    a, t = track_key(title, artist)
    if not t:
        return False
    row = session.exec(
        select(ServerTrack).where(
            ServerTrack.user_id == user_id,
            ServerTrack.artist_norm == a,
            ServerTrack.title_norm == t,
        )
    ).first()
    return row is not None


def record_tracks(session: Session, user_id: int, pairs, *, update_path: bool = False) -> int:
    """Add ``(artist, title[, rel_path])`` entries to the index (issue #21 / #31).

    `pairs` is an iterable of ``(artist, title)`` OR ``(artist, title, rel_path)`` (a
    library-relative POSIX path); both arities are accepted so old 2-tuple callers keep
    working. Returns the number of newly inserted rows (updating/backfilling an existing
    row's path is not counted as new). A pair with no title is skipped — nothing to match on.

    `update_path=True` (used by an authoritative scan) refreshes an existing row's
    `rel_path` to the freshly-found location, so a moved/retagged file isn't later pruned
    as missing (its old path is gone but a valid copy exists under the new one). Delivery
    callers use the default — the first delivered path wins and is never overwritten.
    """
    from app.models import ServerTrack

    existing = {(r.artist_norm, r.title_norm): r for r in session.exec(
        select(ServerTrack).where(ServerTrack.user_id == user_id)).all()}
    added = 0
    for entry in pairs:
        artist, title, *rest = entry           # tolerate a 2- or 3-element tuple/list
        path = rest[0] if rest else None
        a, t = track_key(title, artist)
        if not t:
            continue
        row = existing.get((a, t))
        if row is not None:
            # Backfill a missing path always; overwrite an existing one only for an
            # authoritative scan, where the freshly-found path is the source of truth.
            if path and (not row.rel_path or (update_path and row.rel_path != path)):
                row.rel_path = path
                session.add(row)
            continue
        row = ServerTrack(user_id=user_id, artist_norm=a, title_norm=t, rel_path=path or None)
        session.add(row)
        existing[(a, t)] = row
        added += 1
    return added


# --- WebDAV seed scan ------------------------------------------------------

# Strip a leading playlist-index prefix ("0001 - ") from a filename stem.
_INDEX_PREFIX = re.compile(r"^\s*\d{1,4}\s*-\s*")


def _artist_title_from_path(rel_parts: list[str]) -> tuple[str, str]:
    """Best-effort ``(artist, title)`` from Soundpull's own path layout.

    - ``<artist>/<album>/<title>.<ext>``  → artist + title (album/single uploads)
    - anything shallower (e.g. a playlist folder ``<name>/NNNN - <title>.<ext>``)
      → title only (no reliable artist in the path).
    """
    stem = os.path.splitext(rel_parts[-1])[0]
    title = _INDEX_PREFIX.sub("", stem).strip()
    artist = rel_parts[-3] if len(rel_parts) >= 3 else ""
    return artist, title


# Directory basenames that are caches / internal state, never music — skipped whole
# (case-insensitive) so the scan doesn't PROPFIND the hash-sharded subtrees underneath
# (e.g. an ``attachments/<hash>/…`` store beside the sized-thumbnail cache).
_SKIP_DIR_NAMES = {"attachments", "thumbnails", "previews", "cache"}


def _is_skippable_dir(name: str) -> bool:
    """True for cache / internal-state dirs that never hold music, so the scan can skip
    the whole subtree instead of PROPFINDing thousands of irrelevant folders.

    Covers names starting with ``__`` (e.g. a sized-thumbnail cache ``__sized__/…``) or
    ``.`` (hidden dirs like ``.trash``), plus known cache names in `_SKIP_DIR_NAMES`
    (e.g. a hash-sharded ``attachments/0d/47/5d/…`` store).
    """
    base = name.rstrip("/").rsplit("/", 1)[-1]
    return (base.startswith("__") or base.startswith(".")
            or base.casefold() in _SKIP_DIR_NAMES)


def _walk_remote_files(client, path: str, depth: int, max_depth: int,
                       errors: list | None = None):
    """Yield audio file paths under `path` (recursive, depth-bounded).

    A *sub*-directory whose listing fails is logged and skipped so one unreadable folder
    can't abort the whole scan; the failure is also appended to `errors` (when given) so the
    caller can tell the scan was INCOMPLETE and must not prune the index (issue #31).

    A failure at the **root** (``depth == 0``) is different: the target is unreachable or
    misconfigured, so there is nothing to scan — the exception PROPAGATES rather than being
    swallowed, so `scan_webdav` fails loudly instead of returning a silent empty no-op that
    looks like a healthy but empty library (issue #38).
    """
    try:
        entries = client.ls(path or "", detail=True)
    except Exception as exc:  # noqa: BLE001 - a single unreadable dir must not abort the scan
        if depth == 0:
            raise  # root unreachable → surface as a hard scan failure, not an empty result
        log.warning("scan: listing %r failed: %s", path, exc)
        if errors is not None:
            errors.append((path, str(exc)))
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).rstrip("/")
        if not name or name == path.rstrip("/"):
            continue  # skip empties and the directory's self-entry
        if entry.get("type") == "directory":
            if depth < max_depth and not _is_skippable_dir(name):
                yield from _walk_remote_files(client, name, depth + 1, max_depth, errors)
        elif name.lower().endswith(fix_music_tags._SUPPORTED_EXTS):
            yield name


def _walk_audio_with_lrc(client, path: str, depth: int, max_depth: int,
                         errors: list | None = None):
    """Like `_walk_remote_files`, but yields ``(audio_path, has_lrc)`` per audio file.

    `has_lrc` is whether a sibling ``<stem>.lrc`` already exists — determined from the SAME
    directory listing, so a backfill can skip already-covered tracks without an extra
    per-file existence check. Same error/skip/depth semantics as `_walk_remote_files`.
    """
    try:
        entries = client.ls(path or "", detail=True)
    except Exception as exc:  # noqa: BLE001 - a single unreadable dir must not abort the walk
        if depth == 0:
            raise
        log.warning("backfill: listing %r failed: %s", path, exc)
        if errors is not None:
            errors.append((path, str(exc)))
        return
    files: set[str] = set()
    subdirs: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).rstrip("/")
        if not name or name == path.rstrip("/"):
            continue
        if entry.get("type") == "directory":
            if depth < max_depth and not _is_skippable_dir(name):
                subdirs.append(name)
        else:
            files.add(name)
    for name in files:
        if name.lower().endswith(fix_music_tags._SUPPORTED_EXTS):
            yield name, (name.rsplit(".", 1)[0] + ".lrc") in files
    for sub in subdirs:
        yield from _walk_audio_with_lrc(client, sub, depth + 1, max_depth, errors)


def _prune_missing(session: Session, user_id: int, found_paths: set[str]) -> int:
    """Delete index rows whose stored `rel_path` was not seen in a COMPLETE scan (issue #31).

    Path-based (not key-based), so it's unaffected by artist/title normalisation: a row is
    removed only when its concrete delivered path is no longer on the server (file deleted
    or moved). Rows without a path (e.g. a `mark_existing` seed) are left untouched — there
    is nothing to verify them against. Only safe after an error-free walk; otherwise a
    transiently-unreadable folder would wrongly prune still-present tracks.

    Precondition: `found_paths` and the stored `rel_path`s must share the same frame
    (relative to `webdav_folder`). A moved file self-heals because the scan's `record_tracks`
    ran with `update_path=True` first; a changed `webdav_folder` re-frames everything, so the
    scan prunes the old-frame rows and re-adds them under the new frame in the same run.
    """
    from app.models import ServerTrack

    rows = session.exec(
        select(ServerTrack).where(ServerTrack.user_id == user_id,
                                  ServerTrack.rel_path.is_not(None))
    ).all()
    pruned = 0
    for row in rows:
        if row.rel_path not in found_paths:
            session.delete(row)
            pruned += 1
    return pruned


def scan_webdav(user_id: int, max_depth: int = 8) -> tuple[int, int, list]:
    """Walk the user's WebDAV target folder and reconcile the index with it (issue #21/#31).

    Best-effort and path-based (no remote tag reads): reliably recovers
    ``(artist, title)`` for the album/single layout and title-only for playlist folders.
    The scan is **authoritative** — after an error-free walk it also PRUNES index rows
    whose file is no longer on the server (so deletions/reorganisations self-heal). If any
    directory listing failed, pruning is skipped (an incomplete walk must not delete valid
    rows). Returns ``(added, pruned, errors)`` where ``errors`` is the list of
    ``(path, message)`` sub-directory listing failures — non-empty means the scan was
    INCOMPLETE (no prune) so the caller can warn the user instead of reporting a clean run
    (issue #38). Raises on connection / configuration errors (including an unreachable root).
    """
    from app.db import session_scope
    from app.models import UserSettings
    from app.security import decrypt_secret
    from app.webdav_util import make_client

    with session_scope() as session:
        us = session.exec(select(UserSettings).where(UserSettings.user_id == user_id)).first()
        if not us or not us.webdav_url:
            raise ValueError("Kein WebDAV-Ziel im Profil hinterlegt.")
        url = us.webdav_url
        username = us.webdav_username
        password = decrypt_secret(us.webdav_password_enc) if us.webdav_password_enc else None
        base = (us.webdav_folder or "").strip("/")

    client = make_client(url, username, password)
    prefix = f"{base}/" if base else ""
    pairs: list[tuple[str, str, str]] = []
    found_paths: set[str] = set()
    errors: list = []
    for full in _walk_remote_files(client, base, depth=0, max_depth=max_depth, errors=errors):
        rel = full[len(prefix):] if prefix and full.startswith(prefix) else full
        parts = [p for p in rel.split("/") if p]
        if not parts:
            continue
        found_paths.add(rel)  # every audio file present (even if its title didn't parse)
        artist, title = _artist_title_from_path(parts)
        if title:
            # `rel` is the file's path relative to the WebDAV base folder — the exact
            # frame a playlist m3u references across folders (issue #31).
            pairs.append((artist, title, rel))

    with session_scope() as session:
        # update_path=True: a scan is authoritative, so refresh moved files' paths to the
        # found location before pruning (else a moved file's stale path would be pruned).
        added = record_tracks(session, user_id, pairs, update_path=True)
        pruned = 0
        if errors:
            log.warning("scan: %d directory listing(s) failed — skipping prune so a "
                        "transient error can't delete valid index rows", len(errors))
        else:
            pruned = _prune_missing(session, user_id, found_paths)
    return added, pruned, errors


def backfill_lyrics(user_id: int, progress=None, max_depth: int = 8) -> tuple[int, int, int, list]:
    """Write a `.lrc` sidecar for every library track that lacks one (LRCGET-style backfill).

    Walks the user's WebDAV target folder (path-based, like `scan_webdav`) and, for each audio
    file WITHOUT a sibling `.lrc`, fetches synced lyrics from LRCLIB and uploads the sidecar
    next to the track. Best-effort and WebDAV-only; never touches existing `.lrc` files.

    Returns ``(written, skipped, missing, errors)``:
      - ``written`` — sidecars newly uploaded
      - ``skipped`` — tracks that already had a `.lrc`, or whose artist isn't in the path
        (e.g. a playlist folder) so we can't build a reliable query
      - ``missing`` — queried but LRCLIB had no lyrics
      - ``errors``  — ``(path, message)`` for dir-listing / upload failures (non-empty ⇒ the
        walk was INCOMPLETE)
    Raises on connection/config errors (unreachable root), like `scan_webdav`.
    """
    import io
    from concurrent.futures import ThreadPoolExecutor

    from app import lyrics
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
    prefix = f"{base}/" if base else ""
    errors: list = []

    # 1) Enumerate audio files still missing a sidecar (one listing per dir; `.lrc` inline).
    targets: list[tuple[str, str, str]] = []   # (audio_path, artist, title)
    skipped = 0
    for full, has_lrc in _walk_audio_with_lrc(client, base, 0, max_depth, errors):
        if has_lrc:
            skipped += 1
            continue
        rel = full[len(prefix):] if prefix and full.startswith(prefix) else full
        parts = [p for p in rel.split("/") if p]
        artist, title = _artist_title_from_path(parts)
        if artist and title:
            targets.append((full, artist, title))
        else:
            skipped += 1   # no path-derived artist (e.g. a playlist folder) → can't query

    total = len(targets)
    if progress:
        progress(0, total)

    # 2) Fetch + upload concurrently (bounded — LRCLIB is slow; don't stampede it).
    def handle(item: tuple[str, str, str]) -> str:
        audio_path, artist, title = item
        text = lyrics.fetch_synced_lyrics(artist, title)
        if not text:
            return "missing"
        lrc_path = audio_path.rsplit(".", 1)[0] + ".lrc"
        try:
            client.upload_fileobj(io.BytesIO(text.encode("utf-8")), lrc_path, overwrite=True)
            return "written"
        except Exception as exc:  # noqa: BLE001 - one bad upload must not abort the backfill
            errors.append((lrc_path, str(exc)))
            return "error"

    written = missing = done = 0
    with ThreadPoolExecutor(max_workers=lyrics._MAX_WORKERS) as pool:
        for result in pool.map(handle, targets):
            written += result == "written"
            missing += result == "missing"
            done += 1
            if progress:
                progress(done, total)
    return written, skipped, missing, errors
