"""In-process download worker: a bounded thread pool + live per-job state.

yt-dlp is blocking, so jobs run in a ThreadPoolExecutor capped at
`settings.max_concurrent_downloads`. The UI reads `JobState` (in memory) via a
timer for live progress; the DB row is updated at phase/meta transitions and on
completion (the durable history).
"""
from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select

from app.config import settings
from app.db import session_scope
from app.models import DownloadHistory, UserSettings
from app.pipeline import Destination, Reporter, run_download
from app.security import decrypt_secret

log = logging.getLogger("jobs")

_FINISHED_RETENTION_S = 600  # keep finished jobs visible in the UI this long


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobState:
    id: str
    user_id: int
    url: str
    genre: str
    mode: str
    destination_type: str
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


def _run(job_id: str, url: str, genre: str, mode: str, destination: Destination) -> None:
    js = _registry[job_id]

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

    reporter = Reporter(on_phase=on_phase, on_meta=on_meta, on_track=on_track)

    try:
        result = run_download(job_id=job_id, url=url, genre=genre, mode=mode,
                              destination=destination, reporter=reporter)
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
        with _lock:
            js.phase, js.error, js.finished_at = "error", str(exc), _utcnow()
        _persist(job_id, phase="error", error=str(exc), finished_at=js.finished_at)


def start_job(*, user_id: int, url: str, genre: str, mode: str, destination_type: str) -> str:
    """Queue a download for a user. Returns the job id. Raises on bad config."""
    job_id = uuid.uuid4().hex

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
        session.add(DownloadHistory(
            id=job_id, user_id=user_id, url=url, genre=genre, mode=mode,
            destination_type=destination_type, phase="queued",
        ))

    js = JobState(id=job_id, user_id=user_id, url=url, genre=genre, mode=mode,
                  destination_type=destination_type)
    with _lock:
        _registry[job_id] = js
    _executor.submit(_run, job_id, url, genre, mode, destination)
    return job_id


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
