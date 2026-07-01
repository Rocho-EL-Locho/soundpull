"""In-process download worker: a bounded thread pool + live per-job state.

yt-dlp is blocking, so jobs run in a ThreadPoolExecutor capped at
`settings.max_concurrent_downloads`. The UI reads `JobState` (in memory) via a
timer for live progress; the DB row is updated at phase/meta transitions and on
completion (the durable history).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select

from app import library_index, pipeline
from app.config import settings
from app.db import session_scope
from app.fix_music_tags import TAG_OPTION_FIELDS, TagOptions
from app.models import DownloadHistory, PlaylistSubscription, UserSettings
from app.pipeline import DEFAULT_AUDIO_FORMAT, Destination, Reporter, normalize_audio_format, run_download
from app.security import decrypt_secret

log = logging.getLogger("jobs")


def tag_options_from_settings(us: UserSettings | None) -> TagOptions:
    """Build TagOptions from a UserSettings row (defaults to all-on if absent)."""
    if us is None:
        return TagOptions()
    return TagOptions(**{f: bool(getattr(us, f"tag_{f}")) for f in TAG_OPTION_FIELDS})

_FINISHED_RETENTION_S = 600  # keep finished jobs visible in the UI this long


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# yt-dlp colourises its error messages with ANSI escapes; strip them so the stored /
# displayed error is clean text (they render as garbage in the web UI).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_error(exc: object) -> str:
    return _ANSI_RE.sub("", str(exc)).strip()


@dataclass
class JobState:
    id: str
    user_id: int
    url: str
    genre: str
    mode: str
    destination_type: str
    audio_format: str = DEFAULT_AUDIO_FORMAT
    tag_options: TagOptions = field(default_factory=TagOptions)
    subscription_id: int | None = None   # set for a playlist interval-sync (issue #21)
    phase: str = "queued"
    artist: str | None = None
    album: str | None = None
    current_track: int = 0
    total_tracks: int = 0
    error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None
    result_path: str | None = None   # ZIP path for browser destination
    result_name: str | None = None   # download filename
    summary: str | None = None

    @property
    def finished(self) -> bool:
        return self.phase in ("done", "error")


_registry: dict[str, JobState] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent_downloads))


def _persist(job_id: str, **fields) -> None:
    with session_scope() as session:
        row = session.get(DownloadHistory, job_id)
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)
        session.add(row)


def _record_delivered_safe(job_id: str, user_id: int, delivered: list) -> None:
    """Record delivered tracks into the server index, isolated as a side effect.

    Indexing must never fail an already-completed download/sync — a unique-constraint
    race (two concurrent deliveries of the same track) or a locked DB is logged and
    swallowed rather than propagated (issue #21).
    """
    try:
        with session_scope() as session:
            library_index.record_tracks(session, user_id, delivered)
    except Exception:  # noqa: BLE001 - best-effort; a completed upload stays successful
        log.exception("server-index update failed for %s", job_id)


def _make_reporter(job_id: str, js: JobState) -> Reporter:
    """Wire pipeline callbacks to live JobState + durable DB row (shared by run/sync)."""
    def on_phase(phase: str) -> None:
        with _lock:
            js.phase = phase
        _persist(job_id, phase=phase)

    def on_meta(artist: str, album: str) -> None:
        with _lock:
            js.artist, js.album = artist, album
        _persist(job_id, artist=artist, album=album)

    def on_track(cur: int, tot: int) -> None:
        with _lock:
            js.current_track = cur
            if tot:
                js.total_tracks = tot

    return Reporter(on_phase=on_phase, on_meta=on_meta, on_track=on_track)


def _run(job_id: str, url: str, genre: str, mode: str, destination: Destination,
         audio_format: str, tag_options: TagOptions, cookies_txt: str | None) -> None:
    js = _registry[job_id]
    reporter = _make_reporter(job_id, js)

    try:
        result = run_download(job_id=job_id, url=url, genre=genre, mode=mode,
                              destination=destination, reporter=reporter,
                              audio_format=audio_format, tag_options=tag_options,
                              cookies_txt=cookies_txt)
        # A WebDAV upload actually puts tracks "on the server" → index them so a later
        # playlist sync recognises them (issue #21). Browser ZIPs are not on the server.
        # Best-effort: a failed index write (e.g. a unique-constraint race between two
        # concurrent downloads) must NOT flip a completed upload to "error".
        if destination.type == "webdav" and result.delivered:
            _record_delivered_safe(job_id, js.user_id, result.delivered)
        with _lock:
            js.phase, js.finished_at = "done", _utcnow()
            js.result_path = result.zip_path
            js.result_name = result.zip_name
            js.summary = result.summary
        _persist(job_id, phase="done", finished_at=js.finished_at,
                 artist=js.artist, album=js.album,
                 current_track=js.current_track, total_tracks=js.total_tracks)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        log.exception("download %s failed", job_id)
        err = _clean_error(exc)
        with _lock:
            js.phase, js.error, js.finished_at = "error", err, _utcnow()
        _persist(job_id, phase="error", error=err, finished_at=js.finished_at)


def start_job(*, user_id: int, url: str, genre: str, mode: str, destination_type: str,
              audio_format: str = DEFAULT_AUDIO_FORMAT,
              tag_options: TagOptions | None = None) -> str:
    """Queue a download for a user. Returns the job id. Raises on bad config.

    `tag_options` is the per-download field selection; when omitted it falls back
    to the user's saved defaults (issue #7).
    """
    job_id = uuid.uuid4().hex
    audio_format = normalize_audio_format(audio_format)

    with session_scope() as session:
        us = session.exec(select(UserSettings).where(UserSettings.user_id == user_id)).first()
        destination = Destination(type=destination_type)
        if destination_type == "webdav":
            if not us or not us.webdav_url:
                raise ValueError("Kein WebDAV-Ziel im Profil hinterlegt.")
            destination.webdav_url = us.webdav_url
            destination.webdav_folder = us.webdav_folder
            destination.webdav_username = us.webdav_username
            destination.webdav_password = (
                decrypt_secret(us.webdav_password_enc) if us.webdav_password_enc else None
            )
        # Per-user YouTube cookie (issue #9): decrypt here and pass it through as a
        # call argument only (never stored on JobState/DB, to avoid holding plaintext).
        # Applies to both destinations, so it's independent of destination_type.
        cookies_txt = decrypt_secret(us.youtube_cookies_enc) if us and us.youtube_cookies_enc else None
        if tag_options is None:
            tag_options = tag_options_from_settings(us)
        session.add(DownloadHistory(
            id=job_id, user_id=user_id, url=url, genre=genre, mode=mode,
            audio_format=audio_format, destination_type=destination_type, phase="queued",
        ))

    js = JobState(id=job_id, user_id=user_id, url=url, genre=genre, mode=mode,
                  destination_type=destination_type, audio_format=audio_format,
                  tag_options=tag_options)
    with _lock:
        _registry[job_id] = js
    _executor.submit(_run, job_id, url, genre, mode, destination, audio_format, tag_options, cookies_txt)
    return job_id


# --- Playlist interval-sync (issue #21) ------------------------------------

def is_sync_running(subscription_id: int) -> bool:
    """True if a sync for this subscription is currently queued/running (scheduler guard)."""
    with _lock:
        return any(js.subscription_id == subscription_id and not js.finished
                   for js in _registry.values())


def running_sync_phase(subscription_id: int) -> str | None:
    """Live phase of an in-flight sync for this subscription (for the UI), or None."""
    with _lock:
        for js in _registry.values():
            if js.subscription_id == subscription_id and not js.finished:
                return js.phase
    return None


@dataclass
class _SyncConfig:
    """Everything a sync worker needs, snapshotted from the DB before it starts."""
    subscription_id: int
    user_id: int
    url: str
    genre: str
    audio_format: str
    tag_options: TagOptions
    destination: Destination
    cookies_txt: str | None
    initial_mode: str
    first_run: bool
    existing_tracks: list


def start_sync(subscription_id: int) -> str | None:
    """Queue an interval-sync for a subscription. Returns the job id, or None if it
    can't run (missing/disabled subscription, or no WebDAV target configured).

    Config (WebDAV target, cookie, tag options) is snapshotted from the user's
    current `UserSettings` — a subscription always delivers to WebDAV (issue #21).
    """
    with session_scope() as session:
        sub = session.get(PlaylistSubscription, subscription_id)
        if sub is None or not sub.enabled:
            return None
        us = session.exec(select(UserSettings).where(UserSettings.user_id == sub.user_id)).first()
        sub.last_checked_at = _utcnow()  # claim it now so the scheduler won't re-fire
        if not us or not us.webdav_url:
            sub.last_status = "error"
            sub.last_error = "Kein WebDAV-Ziel im Profil hinterlegt."
            session.add(sub)
            return None
        cfg = _SyncConfig(
            subscription_id=subscription_id,
            user_id=sub.user_id,
            url=sub.url,
            genre=sub.genre,
            audio_format=normalize_audio_format(sub.audio_format),
            tag_options=tag_options_from_settings(us),
            destination=Destination(
                type="webdav", webdav_url=us.webdav_url, webdav_folder=us.webdav_folder,
                webdav_username=us.webdav_username,
                webdav_password=decrypt_secret(us.webdav_password_enc) if us.webdav_password_enc else None,
            ),
            cookies_txt=decrypt_secret(us.youtube_cookies_enc) if us.youtube_cookies_enc else None,
            initial_mode=sub.initial_mode,
            first_run=sub.last_synced_at is None,
            existing_tracks=json.loads(sub.playlist_files) if sub.playlist_files else [],
        )
        session.add(sub)

    job_id = uuid.uuid4().hex
    with session_scope() as session:
        session.add(DownloadHistory(
            id=job_id, user_id=cfg.user_id, url=cfg.url, genre=cfg.genre, mode="playlist",
            audio_format=cfg.audio_format, destination_type="webdav", phase="queued",
        ))
    js = JobState(id=job_id, user_id=cfg.user_id, url=cfg.url, genre=cfg.genre, mode="playlist",
                  destination_type="webdav", audio_format=cfg.audio_format,
                  tag_options=cfg.tag_options, subscription_id=subscription_id)
    with _lock:
        _registry[job_id] = js
    _executor.submit(_run_sync, job_id, cfg)
    return job_id


def _sub_result(session, subscription_id: int, *, status: str, error: str | None = None,
                new_count: int | None = None, playlist_files: list | None = None,
                name: str | None = None) -> None:
    """Persist a sync outcome onto the subscription row."""
    sub = session.get(PlaylistSubscription, subscription_id)
    if sub is None:
        return
    sub.last_status = status
    sub.last_error = error
    if status == "ok":
        sub.last_synced_at = _utcnow()
    if new_count is not None:
        sub.last_new_count = new_count
    if playlist_files is not None:
        sub.playlist_files = json.dumps(playlist_files)
    if name:
        sub.name = name
    session.add(sub)


def _run_sync(job_id: str, cfg: _SyncConfig) -> None:
    js = _registry[job_id]
    reporter = _make_reporter(job_id, js)
    try:
        # "mark existing" first run: seed the index from the current playlist and
        # download nothing, so only FUTURE additions are ever fetched (issue #21).
        if cfg.first_run and cfg.initial_mode == "mark_existing":
            reporter.on_phase("metadata")
            cookie_path = pipeline._write_cookie_file(job_id, cfg.cookies_txt)
            try:
                cookiefile = str(cookie_path) if cookie_path else None
                pl_title, _uploader, _count = pipeline._probe_playlist(cfg.url, cookiefile=cookiefile)
                reporter.on_meta(_uploader, pl_title)
                pairs = pipeline.enumerate_playlist_tracks(
                    cfg.url, cookiefile=cookiefile, limit=settings.max_playlist_items)
            finally:
                if cookie_path:
                    cookie_path.unlink(missing_ok=True)
            _record_delivered_safe(job_id, cfg.user_id, pairs)  # best-effort seed
            with session_scope() as session:
                _sub_result(session, cfg.subscription_id, status="ok", new_count=0,
                            name=pl_title or None)
            summary = f"{len(pairs)} Titel als vorhanden markiert"
        else:
            with session_scope() as session:
                known = library_index.load_index(session, cfg.user_id)

            def on_server(artist: str, title: str) -> bool:
                return library_index.track_key(title, artist) in known

            result = run_download(
                job_id=job_id, url=cfg.url, genre=cfg.genre, mode="playlist",
                destination=cfg.destination, reporter=reporter,
                audio_format=cfg.audio_format, tag_options=cfg.tag_options,
                cookies_txt=cfg.cookies_txt, on_server=on_server,
                existing_tracks=cfg.existing_tracks,
            )
            if result.delivered:  # best-effort; must not roll back the status write below
                _record_delivered_safe(job_id, cfg.user_id, result.delivered)
            with session_scope() as session:
                _sub_result(session, cfg.subscription_id, status="ok",
                            new_count=result.new_track_count,
                            playlist_files=result.playlist_files if result.new_track_count else None,
                            name=result.playlist_name or None)
            summary = result.summary

        with _lock:
            js.phase, js.finished_at, js.summary = "done", _utcnow(), summary
        _persist(job_id, phase="done", finished_at=js.finished_at,
                 artist=js.artist, album=js.album,
                 current_track=js.current_track, total_tracks=js.total_tracks)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        log.exception("sync %s failed", job_id)
        err = _clean_error(exc)
        with _lock:
            js.phase, js.error, js.finished_at = "error", err, _utcnow()
        _persist(job_id, phase="error", error=err, finished_at=js.finished_at)
        with session_scope() as session:
            _sub_result(session, cfg.subscription_id, status="error", error=err)


def _prune_locked() -> None:
    now = _utcnow()
    stale = [
        jid for jid, js in _registry.items()
        if js.finished and js.finished_at
        and (now - js.finished_at).total_seconds() > _FINISHED_RETENTION_S
    ]
    for jid in stale:
        js = _registry.pop(jid, None)
        if js and js.result_path:
            try:
                Path(js.result_path).unlink(missing_ok=True)
            except OSError:
                pass


def get_job(job_id: str) -> JobState | None:
    with _lock:
        return _registry.get(job_id)


def get_user_jobs(user_id: int) -> list[JobState]:
    """Active + recently finished jobs for a user, newest first."""
    with _lock:
        _prune_locked()
        jobs = [js for js in _registry.values() if js.user_id == user_id]
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)
