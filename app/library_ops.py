"""Index-aware library file operations with a trash safety net (roadmap 01).

The foundation for the whole "manage" phase (browser / duplicate finder / health check):
the only way anything in Soundpull is allowed to MODIFY the remote library. Every function
here takes a `user_id`, loads the user's `UserSettings`, builds a WebDAV client, and joins
paths under the user's `webdav_folder` base — after `resolve_rel` has rejected any absolute
path or `..` traversal, so an operation can never escape that base.

Deleting a track does not hard-delete it: unless `trash_retention_days == 0`, the file is
first moved into ``<webdav_folder>/.soundpull-trash/<YYYY-MM-DD>/<original rel path>`` and
hard-deleted only once that dated folder is older than the retention window. The trash
folder starts with ``.`` so `library_index.scan_webdav` already skips it (it never
re-indexes a trashed file). Index rows are kept in sync path-based via
`library_index.remove_by_rel_path` / `update_rel_path` / `record_tracks`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlmodel import select

from app import library_index, webdav_util
from app.webdav_util import make_client, resolve_rel

log = logging.getLogger("library_ops")

# Dated-trash root under the user's `webdav_folder`. Kept in sync with the entry in
# `library_index._SKIP_DIR_NAMES` so the scan skips it.
TRASH_DIR = ".soundpull-trash"


@dataclass
class TrashEntry:
    """One file currently in the trash (paths are relative to `webdav_folder`)."""
    trash_rel: str      # e.g. ".soundpull-trash/2026-07-14/Artist/Album/01 - Song.mp3"
    original_rel: str   # recovered original location: "Artist/Album/01 - Song.mp3"
    date: str           # the dated folder: "2026-07-14"


def trash_rel(rel_path: str, today: date) -> str:
    """Build the trash-relative path a track lands under when trashed on `today`."""
    return f"{TRASH_DIR}/{today.isoformat()}/{rel_path}"


def _original_from_trash(trel: str) -> str:
    """Recover the original library-relative path from a trash-relative one.

    ``.soundpull-trash/<date>/<original…>`` → ``<original…>``. Raises if the shape is wrong.
    """
    parts = [p for p in trel.split("/") if p]
    if len(parts) < 3 or parts[0] != TRASH_DIR:
        raise ValueError(f"Kein gültiger Papierkorb-Pfad: {trel!r}")
    return "/".join(parts[2:])


def _join(base: str, rel: str) -> str:
    """Join a validated relative path onto the (trusted) `webdav_folder` base."""
    return f"{base}/{rel}" if base else rel


def _load(user_id: int):
    """Return ``(client, base, retention_days)`` for the user, or raise if no WebDAV target."""
    from app.db import session_scope
    from app.models import UserSettings
    from app.security import decrypt_secret

    with session_scope() as session:
        us = session.exec(select(UserSettings).where(UserSettings.user_id == user_id)).first()
        if not us or not us.webdav_url:
            raise ValueError("Kein WebDAV-Ziel im Profil hinterlegt.")
        url = us.webdav_url
        username = us.webdav_username
        password = decrypt_secret(us.webdav_password_enc) if us.webdav_password_enc else None
        base = (us.webdav_folder or "").strip("/")
        retention = int(us.trash_retention_days or 0)
    client = make_client(url, username, password)
    return client, base, retention


def _walk_all_files(client, path: str, depth: int = 0, max_depth: int = 10,
                    skip_cache_dirs: bool = False):
    """Yield every file path (any type, recursive, depth-bounded) under `path`.

    Unlike `library_index._walk_remote_files` this keeps NON-audio files too (`.lrc`,
    `.m3u8`, …) — the trash holds whatever was moved into it, not just audio.

    `skip_cache_dirs` prunes the cache / internal-state subtrees `library_index._is_skippable_dir`
    knows about (e.g. oCIS `__sized__`/`attachments` hash shards) — off by default so the trash
    walk keeps its exact semantics, on for a whole-library scan (roadmap 04) that would otherwise
    PROPFIND thousands of irrelevant folders.
    """
    try:
        entries = client.ls(path or "", detail=True)
    except Exception as exc:  # noqa: BLE001 - one unreadable dir must not abort the walk
        if depth == 0:
            raise
        log.warning("trash walk: listing %r failed: %s", path, exc)
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).rstrip("/")
        if not name or name == path.rstrip("/"):
            continue
        if entry.get("type") == "directory":
            if depth < max_depth and not (skip_cache_dirs
                                          and library_index._is_skippable_dir(name)):
                yield from _walk_all_files(client, name, depth + 1, max_depth, skip_cache_dirs)
        else:
            yield name


def _remove_from_index(user_id: int, rel: str) -> None:
    from app.db import session_scope

    with session_scope() as session:
        library_index.remove_by_rel_path(session, user_id, rel)


def _remove_prefix_from_index(user_id: int, folder_rel: str) -> None:
    from app.db import session_scope

    with session_scope() as session:
        library_index.remove_by_prefix(session, user_id, folder_rel)


# --- Public operations -----------------------------------------------------

def trash_track(user_id: int, rel_path: str) -> str | None:
    """Delete a library track safely: move it into the dated trash, drop its index row.

    With ``trash_retention_days == 0`` the file is hard-deleted immediately (no trash) and
    this returns ``None``; otherwise it returns the new trash-relative path. A best-effort
    purge of expired trash runs afterwards.
    """
    rel = resolve_rel(rel_path)
    client, base, retention = _load(user_id)
    src = _join(base, rel)
    if retention <= 0:
        webdav_util.delete_path(client, src)
        _remove_from_index(user_id, rel)
        return None
    trel = trash_rel(rel, date.today())
    webdav_util.move_path(client, src, _join(base, trel), overwrite=True)
    _remove_from_index(user_id, rel)
    try:
        _purge(client, base, retention, force_all=False)
    except Exception as exc:  # noqa: BLE001 - purge is opportunistic, never fail the delete
        log.warning("trash purge after delete failed: %s", exc)
    return trel


def trash_folder(user_id: int, folder_rel: str) -> str | None:
    """Delete a whole album/folder safely: move it into the dated trash, drop its index rows.

    The folder-level counterpart of `trash_track` (roadmap 03). With
    ``trash_retention_days == 0`` the folder is hard-deleted immediately (no trash) and this
    returns ``None``; otherwise it returns the new trash-relative folder path. A best-effort
    purge of expired trash runs afterwards. `folder_rel` must be a non-empty sub-folder — an
    empty value would target the whole library and is rejected.
    """
    rel = resolve_rel(folder_rel).strip("/")
    if not rel:
        raise ValueError("Kein gültiger Ordner zum Löschen.")
    # Refuse a folder that CONTAINS sub-albums: an artist-root "pseudo-album" (loose files
    # beside real sub-albums) would otherwise drag its sibling albums into the trash too
    # (roadmap 03 review). A real album/playlist folder holds its tracks directly → allowed.
    from app.db import session_scope

    with session_scope() as session:
        if library_index.folder_has_nested_tracks(session, user_id, rel):
            raise ValueError("Ordner enthält Unteralben – bitte Titel einzeln löschen.")
    client, base, retention = _load(user_id)
    src = _join(base, rel)
    if retention <= 0:
        webdav_util.delete_path(client, src)
        _remove_prefix_from_index(user_id, rel)
        return None
    trel = trash_rel(rel, date.today())
    webdav_util.move_path(client, src, _join(base, trel), overwrite=True)
    _remove_prefix_from_index(user_id, rel)
    try:
        _purge(client, base, retention, force_all=False)
    except Exception as exc:  # noqa: BLE001 - purge is opportunistic, never fail the delete
        log.warning("trash purge after folder delete failed: %s", exc)
    return trel


def restore_track(user_id: int, trash_rel_path: str) -> str:
    """Move a trashed file back to its original path and re-record it in the index."""
    trel = resolve_rel(trash_rel_path)
    original = _original_from_trash(trel)
    client, base, _ = _load(user_id)
    webdav_util.move_path(client, _join(base, trel), _join(base, original), overwrite=False)
    from app.db import session_scope

    with session_scope() as session:
        parts = [p for p in original.split("/") if p]
        artist, title = library_index._artist_title_from_path(parts)
        library_index.record_tracks(session, user_id, [(artist, title, original)],
                                    update_path=True)
    return original


def move_track(user_id: int, src_rel: str, dst_rel: str) -> None:
    """Move/rename a library file and repoint its index row at the new location."""
    src = resolve_rel(src_rel)
    dst = resolve_rel(dst_rel)
    client, base, _ = _load(user_id)
    webdav_util.move_path(client, _join(base, src), _join(base, dst), overwrite=False)
    from app.db import session_scope

    with session_scope() as session:
        library_index.update_rel_path(session, user_id, src, dst)


_M3U_EXTS = (".m3u8", ".m3u")


def list_playlist_files(user_id: int) -> list[str]:
    """Every ``.m3u8``/``.m3u`` in the library, as paths relative to `webdav_folder` (roadmap 04).

    The candidate set the duplicate finder rewrites when a trashed track was referenced by a
    playlist (issue #31 cross-folder references). Uses the trash-aware `_walk_all_files` walker
    (keeps non-audio files); the `.soundpull-trash` subtree lives under a dot-prefixed folder,
    but that walker does not skip it — the caller only reads/edits *real* playlist folders, and
    a stale trashed m3u is harmless to leave untouched.
    """
    client, base, _ = _load(user_id)
    prefix = f"{base}/" if base else ""
    out: list[str] = []
    # skip_cache_dirs prunes cache/attachment shards AND the dot-prefixed `.soundpull-trash`
    # subtree, so a trashed m3u is never picked up and huge cache trees aren't PROPFINDed.
    for full in _walk_all_files(client, base, skip_cache_dirs=True):
        if not full.lower().endswith(_M3U_EXTS):
            continue
        rel = full[len(prefix):] if prefix and full.startswith(prefix) else full
        out.append(rel)
    return out


def read_library_text(user_id: int, rel_path: str) -> str:
    """Download a small library text file (e.g. an `.m3u8`) and decode it (roadmap 04)."""
    rel = resolve_rel(rel_path)
    client, base, _ = _load(user_id)
    return webdav_util.read_text(client, _join(base, rel))


def write_library_text(user_id: int, rel_path: str, text: str) -> None:
    """Overwrite a small library text file (roadmap 04 — playlist m3u repair)."""
    rel = resolve_rel(rel_path)
    client, base, _ = _load(user_id)
    webdav_util.write_text(client, _join(base, rel), text)


def list_trash(user_id: int) -> list[TrashEntry]:
    """Enumerate every file currently in the user's trash (newest dated folder last)."""
    client, base, _ = _load(user_id)
    root = _join(base, TRASH_DIR)
    if not webdav_util.path_exists(client, root):
        return []
    prefix = f"{base}/" if base else ""
    entries: list[TrashEntry] = []
    for full in _walk_all_files(client, root):
        rel = full[len(prefix):] if prefix and full.startswith(prefix) else full
        parts = [p for p in rel.split("/") if p]
        if len(parts) < 3 or parts[0] != TRASH_DIR:
            continue
        entries.append(TrashEntry(trash_rel=rel, original_rel="/".join(parts[2:]),
                                  date=parts[1]))
    return sorted(entries, key=lambda e: e.trash_rel)


def purge_trash(user_id: int, *, force_all: bool = False) -> int:
    """Hard-delete trash folders older than the retention window (or ALL with `force_all`).

    Returns the number of dated folders removed. The cutoff is read from the DATED FOLDER
    NAME (``<TRASH_DIR>/<YYYY-MM-DD>/…``) so no per-file metadata is needed.
    """
    client, base, retention = _load(user_id)
    return _purge(client, base, retention, force_all=force_all)


def _purge(client, base: str, retention: int, *, force_all: bool) -> int:
    root = _join(base, TRASH_DIR)
    if not webdav_util.path_exists(client, root):
        return 0
    cutoff = date.today() - timedelta(days=max(retention, 0))
    removed = 0
    for name, full in webdav_util.list_dirs(client, root):
        try:
            folder_date = date.fromisoformat(name)
        except ValueError:
            continue  # not a dated folder — leave it untouched
        if force_all or folder_date < cutoff:
            webdav_util.delete_path(client, full)
            removed += 1
    return removed
