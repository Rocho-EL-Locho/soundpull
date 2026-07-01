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


def record_tracks(session: Session, user_id: int, pairs) -> int:
    """Add ``(artist, title)`` pairs to the index, skipping ones already present.

    `pairs` is an iterable of raw ``(artist, title)`` tuples (normalised here via
    `track_key`). Returns the number of newly inserted rows. A pair with no title is
    skipped — there is nothing to match on.
    """
    from app.models import ServerTrack

    known = load_index(session, user_id)
    added = 0
    for artist, title in pairs:
        a, t = track_key(title, artist)
        if not t or (a, t) in known:
            continue
        session.add(ServerTrack(user_id=user_id, artist_norm=a, title_norm=t))
        known.add((a, t))
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


def _walk_remote_files(client, path: str, depth: int, max_depth: int):
    """Yield audio file paths under `path` (recursive, depth-bounded)."""
    try:
        entries = client.ls(path or "", detail=True)
    except Exception as exc:  # noqa: BLE001 - a single unreadable dir must not abort the scan
        log.warning("scan: listing %r failed: %s", path, exc)
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).rstrip("/")
        if not name or name == path.rstrip("/"):
            continue  # skip empties and the directory's self-entry
        if entry.get("type") == "directory":
            if depth < max_depth:
                yield from _walk_remote_files(client, name, depth + 1, max_depth)
        elif name.lower().endswith(fix_music_tags._SUPPORTED_EXTS):
            yield name


def scan_webdav(user_id: int, max_depth: int = 8) -> int:
    """Walk the user's WebDAV target folder and seed the index from file paths.

    Best-effort and path-based (no remote tag reads): reliably recovers
    ``(artist, title)`` for the album/single layout and title-only for playlist
    folders. Returns the number of newly indexed tracks. Raises on connection /
    configuration errors so the caller can surface them.
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
    pairs: list[tuple[str, str]] = []
    for full in _walk_remote_files(client, base, depth=0, max_depth=max_depth):
        rel = full[len(prefix):] if prefix and full.startswith(prefix) else full
        parts = [p for p in rel.split("/") if p]
        if not parts:
            continue
        artist, title = _artist_title_from_path(parts)
        if title:
            pairs.append((artist, title))

    with session_scope() as session:
        return record_tracks(session, user_id, pairs)
