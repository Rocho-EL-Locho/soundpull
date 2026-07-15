"""Per-user library / history / settings export & backup (roadmap 17).

Pure, UI-free serializers that turn a user's own data into downloadable files, plus a
settings-import that merges a previously exported JSON back onto the user's settings:

- `library_manifest_csv` / `library_manifest_json` — the `ServerTrack` index ("what do I own"),
  CSV columns ``artist,title,album,rel_path`` (the leading ``artist,title`` is the feature-12
  batch-import contract).
- `history_csv` — the `DownloadHistory` ("where did this come from").
- `settings_json` — the user's `UserSettings`, restricted to an explicit **allowlist** of
  non-secret config fields. The four Fernet-encrypted secrets (``*_enc``) can NEVER appear (an
  allowlist can't leak a future secret column by accident; a test also deny-scans the output).
- `apply_settings_json` — merges an uploaded JSON: only allowlisted keys, each type-checked
  against the model; unknown / wrong-typed / secret keys are skipped, never written.

**Encoding:** the CSVs are emitted with a leading UTF-8 BOM so Excel/LibreOffice render umlauts
correctly; the JSON exports carry no BOM. Everything is per-user and delivered via the browser
(`ui.download`) — nothing here writes into the music library, and there are no pipeline/model
changes, so metadata parity is untouched.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import select

from app.library_index import _artist_title_from_path, split_rel_path

log = logging.getLogger("exports")

_BOM = "﻿"  # UTF-8 BOM → Excel/LibreOffice auto-detect UTF-8 (umlauts intact)

# Exportable + importable UserSettings fields → value category for import type-checking.
# EXPLICIT allowlist: the four `*_enc` secrets, ids and runtime timestamps are intentionally
# absent, so neither an export nor a future-added secret column can ever leak a secret.
#   bool     — must be a JSON bool
#   int      — must be a JSON int (not bool), clamped ≥0 where noted
#   str      — must be a JSON string (non-nullable column)
#   str_opt  — string or null (nullable column)
_FIELD_KINDS: dict[str, str] = {
    "default_genre": "str", "default_mode": "str", "default_audio_format": "str",
    "destination_type": "str", "language": "str",
    "tag_genre": "bool", "tag_album_artist": "bool", "tag_cover": "bool",
    "tag_track_number": "bool", "tag_feat_artist": "bool", "tag_comments": "bool",
    "webdav_url": "str_opt", "webdav_folder": "str_opt", "webdav_username": "str_opt",
    "dedup_skip_existing": "bool", "fetch_synced_lyrics": "bool",
    "trash_retention_days": "int", "library_scan_interval_hours": "int",
    "navidrome_base_url": "str",
    "notify_new_tracks": "bool", "notify_sync_error": "bool", "notify_download_error": "bool",
    "notify_ntfy_url": "str_opt", "notify_webhook_url": "str_opt", "notify_email_to": "str_opt",
    "notify_smtp_host": "str_opt", "notify_smtp_port": "int", "notify_smtp_user": "str_opt",
    "notify_smtp_from": "str_opt", "notify_smtp_security": "str",
}
_NON_NEGATIVE = {"trash_retention_days", "library_scan_interval_hours", "notify_smtp_port"}

_LIBRARY_HEADER = ["artist", "title", "album", "rel_path"]
_HISTORY_HEADER = ["id", "url", "mode", "genre", "audio_format", "destination_type",
                   "artist", "album", "phase", "total_tracks", "failed_tracks", "error",
                   "created_at", "finished_at"]


@dataclass
class ImportResult:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --- library manifest -------------------------------------------------------

def _library_rows(user_id: int) -> list[dict]:
    from app.db import session_scope
    from app.models import ServerTrack

    with session_scope() as session:
        tracks = session.exec(
            select(ServerTrack).where(ServerTrack.user_id == user_id)
            .order_by(ServerTrack.rel_path, ServerTrack.artist_norm, ServerTrack.title_norm)
        ).all()
        rows = []
        for tr in tracks:
            if tr.rel_path:
                artist, album, _fn = split_rel_path(tr.rel_path)
                _a, title = _artist_title_from_path([p for p in tr.rel_path.split("/") if p])
                rows.append({"artist": artist, "title": title or tr.title_norm,
                             "album": album, "rel_path": tr.rel_path})
            else:  # a mark_existing seed with no known path → fall back to the normalized keys
                rows.append({"artist": tr.artist_norm, "title": tr.title_norm,
                             "album": "", "rel_path": ""})
    return rows


def library_manifest_csv(user_id: int) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_LIBRARY_HEADER)
    writer.writeheader()
    writer.writerows(_library_rows(user_id))
    return _BOM + buf.getvalue()


def library_manifest_json(user_id: int) -> str:
    return json.dumps(_library_rows(user_id), ensure_ascii=False, indent=2)


# --- download history -------------------------------------------------------

def history_csv(user_id: int) -> str:
    from app.db import session_scope
    from app.models import DownloadHistory

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_HISTORY_HEADER, extrasaction="ignore")
    writer.writeheader()
    with session_scope() as session:
        rows = session.exec(
            select(DownloadHistory).where(DownloadHistory.user_id == user_id)
            .order_by(DownloadHistory.created_at)
        ).all()
        for h in rows:
            writer.writerow({
                "id": h.id, "url": h.url, "mode": h.mode, "genre": h.genre,
                "audio_format": h.audio_format, "destination_type": h.destination_type,
                "artist": h.artist or "", "album": h.album or "", "phase": h.phase,
                "total_tracks": h.total_tracks, "failed_tracks": h.failed_tracks,
                "error": h.error or "",
                "created_at": _iso(h.created_at), "finished_at": _iso(h.finished_at),
            })
    return _BOM + buf.getvalue()


def _iso(dt) -> str:
    return dt.isoformat() if isinstance(dt, datetime) else ""


# --- settings export / import ----------------------------------------------

def settings_json(user_id: int) -> str:
    """Serialize the user's non-secret settings (allowlist only). Never emits a secret field."""
    from app.db import session_scope
    from app.models import UserSettings

    with session_scope() as session:
        us = session.exec(select(UserSettings).where(UserSettings.user_id == user_id)).first()
        data = {} if us is None else {f: getattr(us, f) for f in _FIELD_KINDS}
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _type_ok(kind: str, value) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "str":
        return isinstance(value, str)
    if kind == "str_opt":
        return value is None or isinstance(value, str)
    return False


def apply_settings_json(user_id: int, payload: str) -> ImportResult:
    """Merge an exported settings JSON back onto the user's settings (allowlist + type-checked).

    Only allowlisted, correctly-typed keys are written; unknown keys, wrong types and any secret
    field (never in the allowlist) go to `skipped`. Secrets are therefore impossible to import even
    from a hostile payload. Mirrors the settings-page `save()` write path.
    """
    from app.db import session_scope
    from app.models import UserSettings

    result = ImportResult()
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as exc:
        result.errors.append(f"invalid JSON: {exc}")
        return result
    if not isinstance(data, dict):
        result.errors.append("expected a JSON object")
        return result

    with session_scope() as session:
        row = session.exec(select(UserSettings).where(UserSettings.user_id == user_id)).first()
        if row is None:
            row = UserSettings(user_id=user_id)
            session.add(row)
        for key, value in data.items():
            kind = _FIELD_KINDS.get(key)
            if kind is None:                       # unknown OR a secret/runtime field → never write
                result.skipped.append(key)
                continue
            if not _type_ok(kind, value):
                result.skipped.append(key)
                continue
            if kind == "int" and key in _NON_NEGATIVE:
                value = max(int(value), 0)
            setattr(row, key, value)
            result.applied.append(key)
        if result.applied:
            from datetime import timezone
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
    return result
