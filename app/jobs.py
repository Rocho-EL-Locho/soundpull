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

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app import library_index, notifications, pipeline
from app.config import settings
from app.db import session_scope
from app.fix_music_tags import TAG_OPTION_FIELDS, TagOptions
from app.models import DownloadHistory, PlaylistSubscription, UserSettings
from app.pipeline import (
    DEFAULT_AUDIO_FORMAT, Destination, Reporter, normalize_audio_format, run_artist_download,
    run_download,
)
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


# Non-fatal warnings surfaced on a completed job when the server-index write failed (#38).
# These are i18n KEYS, not text — the worker runs off the request thread where `t()` can't
# resolve the active language, so the page resolves them via `t()` at render time. `t()`
# returns an unknown string unchanged, so storing a key stays backward-safe.
_INDEX_WARNING_KEY = "jobs.index_update_failed"  # delivery OK, but the index wasn't updated
_SEED_FAILED_KEY = "jobs.seed_failed"            # mark_existing seed couldn't be persisted
_PARTIAL_KEY = "jobs.partial_delivery"           # some tracks failed (throttle/403) → partial


def _delivery_warning(result, index_ok: bool = True) -> tuple[str | None, int, int]:
    """Pick the job's non-fatal warning from a delivery Result: (key, total, failed).

    A partial delivery (tracks silently dropped by YouTube throttling/403, or files the
    WebDAV server rejected) is the most important thing to surface — it makes an album that
    reports "done" but is missing tracks visible instead of a silent success. It outranks the
    stale-index note (#38). `total`/`failed` are carried so the page can render "N von M"."""
    failed = result.failed_count + result.upload_failed_count
    if failed > 0:
        total = result.expected_count or (result.new_track_count + failed)
        return _PARTIAL_KEY, total, failed
    return (None if index_ok else _INDEX_WARNING_KEY), 0, 0


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
    failed_tracks: int = 0   # tracks/files that failed (throttle/403/upload) → partial delivery
    # Album-level progress for an artist run (issue #32); 0 for other modes.
    current_album: int = 0
    total_albums: int = 0
    error: str | None = None
    warning: str | None = None   # non-fatal note on a done job (e.g. index update failed, #38)
    log_lines: list[str] = field(default_factory=list)  # event timeline (issue #44)
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


def _log_event(js: JobState, message: str) -> None:
    """Append a timestamped line to the job's event timeline and persist it (issue #44).

    Best-effort: the timeline is a non-essential diagnostic, so a failed write is logged and
    swallowed — it must never fail or flip a job (mirroring the notification/cover/lyrics side
    effects; a raise here inside a terminal block would otherwise turn a delivered job into an
    "error", skip its notification, or orphan a queued job). `_lock` guards the append + join
    because an artist run forwards `on_meta` from several parallel album threads, so `log_lines`
    can be touched concurrently; the DB write happens outside the lock. The joined text lands in
    `DownloadHistory.log` for the detail dialog, surviving after the JobState is pruned.
    """
    try:
        entry = f"[{_utcnow().strftime('%H:%M:%S')}] {message}"
        with _lock:
            js.log_lines.append(entry)
            blob = "\n".join(js.log_lines)
        _persist(js.id, log=blob)
    except Exception:  # noqa: BLE001 - a diagnostic log must never affect the job
        log.exception("log-event persist failed for %s", js.id)


def _record_delivered_safe(job_id: str, user_id: int, delivered: list) -> bool:
    """Record delivered tracks into the server index, isolated as a side effect.

    Indexing must never *fail* an already-completed download/sync — a locked DB or a
    unique-constraint race is logged and swallowed rather than propagated (issue #21).
    Returns ``True`` when the index reflects the tracks, ``False`` when the write genuinely
    failed — the caller keeps the job ``done`` but surfaces a warning so the stale-index
    risk is visible (issue #38).

    A `UniqueConstraint` race (a concurrent delivery inserted an overlapping
    ``(user, artist, title)`` key, rolling back our whole batch) is **benign**: we retry
    once, and on the retry `record_tracks` sees the now-committed rows, skips them, and
    still inserts our genuinely-new ones. So a benign race resolves to ``True`` (no false
    warning) instead of dropping the rest of the batch — only a persistent conflict warns.
    """
    for attempt in (1, 2):
        try:
            with session_scope() as session:
                library_index.record_tracks(session, user_id, delivered)
            return True
        except IntegrityError:
            if attempt == 1:
                continue  # a concurrent insert won the race → re-record the remainder
            log.exception("server-index update kept conflicting for %s", job_id)
            return False
        except Exception:  # noqa: BLE001 - best-effort; a completed upload stays successful
            log.exception("server-index update failed for %s", job_id)
            return False
    return False  # unreachable (the loop always returns), kept for a total function


def _notify_safe(user_id: int, dispatch) -> None:
    """Fire a notification for a background event (issue #42). Best-effort, off-thread.

    Loads the user's current notification config fresh from the DB, then hands it to
    `dispatch(cfg)` (a small closure that calls a `notifications.notify_*` function). Runs
    on its OWN daemon thread — never the download pool — so a slow/unreachable channel can't
    delay the job or hold a worker; any error (config load, decrypt, network) is swallowed.
    """
    def _run() -> None:
        try:
            with session_scope() as session:
                us = session.exec(
                    select(UserSettings).where(UserSettings.user_id == user_id)).first()
                cfg = notifications.NotifyConfig.from_settings(us) if us else None
            if cfg is not None:
                dispatch(cfg)
        except Exception:  # noqa: BLE001 - a notification must never affect the job
            log.exception("notification dispatch failed for user %s", user_id)

    threading.Thread(target=_run, name="notify", daemon=True).start()


def _make_reporter(job_id: str, js: JobState) -> Reporter:
    """Wire pipeline callbacks to live JobState + durable DB row (shared by run/sync)."""
    def on_phase(phase: str) -> None:
        # on_phase fires on EVERY yt-dlp progress tick (pipeline progress_hook), so persist and
        # log only on an actual transition — otherwise the timeline floods with thousands of
        # identical "download" lines and the row is rewritten per tick (issue #44).
        with _lock:
            changed = js.phase != phase
            js.phase = phase
        if changed:
            _persist(job_id, phase=phase)
            _log_event(js, phase)

    def on_meta(artist: str, album: str) -> None:
        with _lock:
            js.artist, js.album = artist, album
        _persist(job_id, artist=artist, album=album)
        _log_event(js, f"{artist or '?'} — {album or '?'}")

    def on_track(cur: int, tot: int) -> None:
        with _lock:
            js.current_track = cur
            if tot:
                js.total_tracks = tot

    return Reporter(on_phase=on_phase, on_meta=on_meta, on_track=on_track)


def _make_artist_reporter(job_id: str, js: JobState) -> Reporter:
    """Reporter for an artist run (issue #32): base wiring + two-level album progress."""
    base = _make_reporter(job_id, js)

    def on_album(current: int, total: int, name: str) -> None:
        # `name` is "" only for the initial total-publish before fan-out; don't blank the album
        # label (or hit the DB) for it. With a concurrent album pool `current` is a completion
        # count, so the bar stays monotonic even though releases finish out of order.
        with _lock:
            js.current_album, js.total_albums = current, total
            if name:
                js.album = name
        if name:
            _persist(job_id, album=name)
            _log_event(js, f"album {current}/{total}: {name}")

    return Reporter(on_phase=base.on_phase, on_meta=base.on_meta,
                    on_track=base.on_track, on_album=on_album)


def _run_artist(job_id: str, url: str, genre: str, destination: Destination,
                audio_format: str, tag_options: TagOptions, cookies_txt: str | None,
                dedup: bool = True, fetch_lyrics: bool = False) -> None:
    js = _registry[job_id]
    reporter = _make_artist_reporter(job_id, js)

    try:
        # Artist runs default to reconciling against the server on WebDAV (auto-dedup, issue
        # #31): a re-download of a big discography then only pulls tracks not already in the
        # library, instead of re-processing all N releases. The per-download `dedup` toggle can
        # turn this off to force a full re-download. A browser ZIP has no library to dedup
        # against, so it stays a plain full download regardless. `existing_ref` is playlist-only
        # (m3u cross-refs), so it is unused here (albums write no m3u).
        on_server = None
        if dedup and destination.type == "webdav":
            with session_scope() as session:
                paths = library_index.load_index_paths(session, js.user_id)
            on_server = lambda a, t: library_index.track_key(t, a) in paths  # noqa: E731

        # Cap the parallel album pool to a sane 1–4 regardless of how the env var is set.
        album_concurrency = max(1, min(4, settings.max_artist_album_concurrency))
        result = run_artist_download(job_id=job_id, url=url, genre=genre,
                                     destination=destination, reporter=reporter,
                                     audio_format=audio_format, tag_options=tag_options,
                                     cookies_txt=cookies_txt, on_server=on_server,
                                     max_items=settings.max_artist_items,
                                     album_concurrency=album_concurrency,
                                     fetch_lyrics=fetch_lyrics)
        indexed = True
        if destination.type == "webdav" and result.delivered:
            indexed = _record_delivered_safe(job_id, js.user_id, result.delivered)
        warning, total, failed = _delivery_warning(result, indexed)
        with _lock:
            js.phase, js.finished_at = "done", _utcnow()
            js.result_path = result.zip_path
            js.result_name = result.zip_name
            js.summary = result.summary
            js.warning = warning
            js.failed_tracks = failed
            if total:
                js.total_tracks = total
        _persist(job_id, phase="done", finished_at=js.finished_at, warning=warning,
                 artist=js.artist, album=js.album, failed_tracks=failed,
                 current_track=js.current_track, total_tracks=js.total_tracks)
        _log_event(js, f"done, {failed}/{total} missing" if failed else "done")
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        log.exception("artist download %s failed", job_id)
        err = _clean_error(exc)
        with _lock:
            js.phase, js.error, js.finished_at = "error", err, _utcnow()
        _persist(job_id, phase="error", error=err, finished_at=js.finished_at)
        _log_event(js, f"error: {err}")
        _notify_safe(js.user_id, lambda c: notifications.notify_error(
            c, kind="download", url=url, error=err))


def _run(job_id: str, url: str, genre: str, mode: str, destination: Destination,
         audio_format: str, tag_options: TagOptions, cookies_txt: str | None,
         dedup: bool = False, fetch_lyrics: bool = False) -> None:
    js = _registry[job_id]
    reporter = _make_reporter(job_id, js)

    try:
        # Dedup (issue #31): skip tracks already in the user's library and reference the
        # existing copy in a playlist's m3u. WebDAV-only — a browser ZIP has no library to
        # dedup against / reference into. Both closures share one loaded index snapshot.
        on_server = existing_ref = None
        if dedup and destination.type == "webdav":
            with session_scope() as session:
                paths = library_index.load_index_paths(session, js.user_id)
            on_server = lambda a, t: library_index.track_key(t, a) in paths  # noqa: E731
            existing_ref = lambda a, t: paths.get(library_index.track_key(t, a))  # noqa: E731

        result = run_download(job_id=job_id, url=url, genre=genre, mode=mode,
                              destination=destination, reporter=reporter,
                              audio_format=audio_format, tag_options=tag_options,
                              cookies_txt=cookies_txt, on_server=on_server,
                              existing_ref=existing_ref, fetch_lyrics=fetch_lyrics)
        # A WebDAV upload actually puts tracks "on the server" → index them so a later
        # playlist sync recognises them (issue #21). Browser ZIPs are not on the server.
        # Best-effort: a failed index write (e.g. a unique-constraint race between two
        # concurrent downloads) must NOT flip a completed upload to "error".
        indexed = True
        if destination.type == "webdav" and result.delivered:
            indexed = _record_delivered_safe(job_id, js.user_id, result.delivered)
        warning, total, failed = _delivery_warning(result, indexed)
        with _lock:
            js.phase, js.finished_at = "done", _utcnow()
            js.result_path = result.zip_path
            js.result_name = result.zip_name
            js.summary = result.summary
            js.warning = warning
            js.failed_tracks = failed
            if total:
                js.total_tracks = total
        _persist(job_id, phase="done", finished_at=js.finished_at, warning=warning,
                 artist=js.artist, album=js.album, failed_tracks=failed,
                 current_track=js.current_track, total_tracks=js.total_tracks)
        _log_event(js, f"done, {failed}/{total} missing" if failed else "done")
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        log.exception("download %s failed", job_id)
        err = _clean_error(exc)
        with _lock:
            js.phase, js.error, js.finished_at = "error", err, _utcnow()
        _persist(job_id, phase="error", error=err, finished_at=js.finished_at)
        _log_event(js, f"error: {err}")
        _notify_safe(js.user_id, lambda c: notifications.notify_error(
            c, kind="download", url=url, error=err))


def start_job(*, user_id: int, url: str, genre: str, mode: str, destination_type: str,
              audio_format: str = DEFAULT_AUDIO_FORMAT,
              tag_options: TagOptions | None = None, dedup: bool = False,
              fetch_lyrics: bool = False) -> str:
    """Queue a download for a user. Returns the job id. Raises on bad config.

    `tag_options` is the per-download field selection; when omitted it falls back
    to the user's saved defaults (issue #7). `dedup` skips tracks already in the
    user's library and references them in a playlist m3u (issue #31); it only takes
    effect for the WebDAV destination. `fetch_lyrics` writes a best-effort `.lrc`
    synced-lyrics sidecar next to each track (issue #43); applies to both destinations.
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
    _log_event(js, "queued")
    if mode == "artist":
        # An artist run (issue #32) fans out into N album downloads under one job. It defaults
        # to auto-dedup on WebDAV (skip tracks already in the library) but honours the
        # per-download `dedup` toggle, so the user can force a full re-download.
        _executor.submit(_run_artist, job_id, url, genre, destination, audio_format,
                         tag_options, cookies_txt, dedup, fetch_lyrics)
    else:
        _executor.submit(_run, job_id, url, genre, mode, destination, audio_format,
                         tag_options, cookies_txt, dedup, fetch_lyrics)
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
    fetch_lyrics: bool = False


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
            fetch_lyrics=bool(us.fetch_synced_lyrics),
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
    _log_event(js, "queued")
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
        warning, total, failed = None, 0, 0  # set below: index-fail (#38) or partial delivery
        new_count, playlist_label = 0, ""    # for the "new tracks" notification (issue #42)
        # "mark existing" first run: seed the index from the current playlist and
        # download nothing, so only FUTURE additions are ever fetched (issue #21).
        if cfg.first_run and cfg.initial_mode == "mark_existing":
            reporter.on_phase("metadata")
            cookie_path = pipeline._write_cookie_file(job_id, cfg.cookies_txt)
            try:
                cookiefile = str(cookie_path) if cookie_path else None
                pl_title, _uploader, _count, _pl_id = pipeline._probe_playlist(
                    cfg.url, cookiefile=cookiefile)
                reporter.on_meta(_uploader, pl_title)
                pairs = pipeline.enumerate_playlist_tracks(
                    cfg.url, cookiefile=cookiefile, limit=settings.max_playlist_items)
            finally:
                if cookie_path:
                    cookie_path.unlink(missing_ok=True)
            seeded = _record_delivered_safe(job_id, cfg.user_id, pairs)
            with session_scope() as session:
                if seeded:
                    _sub_result(session, cfg.subscription_id, status="ok", new_count=0,
                                name=pl_title or None)
                else:
                    # The seed WRITE failed → the index is NOT populated. Do not mark the
                    # subscription synced: that would leave first_run False, so the next sync
                    # treats the whole playlist as new and re-downloads ALL of it. Record an
                    # error instead (last_synced_at stays None → it re-seeds next interval) (#38).
                    warning = _SEED_FAILED_KEY
                    _sub_result(session, cfg.subscription_id, status="error",
                                error=_SEED_FAILED_KEY, name=pl_title or None)
            summary = f"{len(pairs)} Titel als vorhanden markiert"
        else:
            # One loaded index snapshot serves both the skip decision and, for a track
            # that lives elsewhere in the library (e.g. an album track), the cross-folder
            # m3u reference so the synced playlist stays complete without a duplicate (#31).
            with session_scope() as session:
                paths = library_index.load_index_paths(session, cfg.user_id)

            def on_server(artist: str, title: str) -> bool:
                return library_index.track_key(title, artist) in paths

            def existing_ref(artist: str, title: str) -> str | None:
                return paths.get(library_index.track_key(title, artist))

            result = run_download(
                job_id=job_id, url=cfg.url, genre=cfg.genre, mode="playlist",
                destination=cfg.destination, reporter=reporter,
                audio_format=cfg.audio_format, tag_options=cfg.tag_options,
                cookies_txt=cfg.cookies_txt, on_server=on_server, existing_ref=existing_ref,
                existing_tracks=cfg.existing_tracks, fetch_lyrics=cfg.fetch_lyrics,
            )
            index_ok = True
            if result.delivered:
                # Delivery succeeded and the manifest is saved below, so the sync stays "ok";
                # a stale ServerTrack index just means those tracks re-download next sync.
                # Surface it on the job (durable in the history), not on the subscription (#38).
                index_ok = _record_delivered_safe(job_id, cfg.user_id, result.delivered)
            warning, total, failed = _delivery_warning(result, index_ok)
            with session_scope() as session:
                # Persist whenever we have a manifest (new downloads OR new references), so
                # the complete rebuilt playlist — including cross-folder refs — is kept (#31).
                _sub_result(session, cfg.subscription_id, status="ok",
                            new_count=result.new_track_count,
                            playlist_files=result.playlist_files or None,
                            name=result.playlist_name or None)
            summary = result.summary
            new_count = result.new_track_count
            playlist_label = result.playlist_name or js.album or ""

        with _lock:
            js.phase, js.finished_at, js.summary = "done", _utcnow(), summary
            js.warning = warning
            js.failed_tracks = failed
            if total:
                js.total_tracks = total
        _persist(job_id, phase="done", finished_at=js.finished_at, warning=warning,
                 artist=js.artist, album=js.album, failed_tracks=failed,
                 current_track=js.current_track, total_tracks=js.total_tracks)
        _log_event(js, f"done, {failed}/{total} missing" if failed else "done")
        if new_count > 0:
            _notify_safe(cfg.user_id, lambda c: notifications.notify_new_tracks(
                c, playlist=playlist_label, count=new_count))
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        log.exception("sync %s failed", job_id)
        err = _clean_error(exc)
        with _lock:
            js.phase, js.error, js.finished_at = "error", err, _utcnow()
        _persist(job_id, phase="error", error=err, finished_at=js.finished_at)
        _log_event(js, f"error: {err}")
        with session_scope() as session:
            _sub_result(session, cfg.subscription_id, status="error", error=err)
        _notify_safe(cfg.user_id, lambda c: notifications.notify_error(
            c, kind="sync", url=cfg.url, error=err))


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
