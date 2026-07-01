"""In-process scheduler for playlist interval-sync (issue #21).

A single daemon thread wakes every `settings.sync_tick_seconds`, finds subscriptions
whose interval has elapsed, and enqueues a sync via `app.jobs` (reusing the same
bounded worker pool as manual downloads). No external cron — self-contained and
Docker-friendly. Started/stopped from `app.main` via NiceGUI's on_startup/on_shutdown.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.config import settings
from app.db import session_scope
from app.models import PlaylistSubscription

log = logging.getLogger("scheduler")

_stop = threading.Event()
_thread: threading.Thread | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_due(sub: PlaylistSubscription, now: datetime) -> bool:
    """True if `sub` should be synced now (never-run → due; else interval elapsed)."""
    if not sub.enabled:
        return False
    if sub.last_checked_at is None:
        return True
    last = sub.last_checked_at
    if last.tzinfo is None:  # SQLite hands back naive datetimes — treat as UTC
        last = last.replace(tzinfo=timezone.utc)
    interval = max(1, int(sub.interval_hours or 24))
    return now - last >= timedelta(hours=interval)


def _tick() -> None:
    from app.jobs import is_sync_running, start_sync

    now = _utcnow()
    with session_scope() as session:
        subs = session.exec(select(PlaylistSubscription)).all()
        due = [s.id for s in subs if _is_due(s, now)]
    for sid in due:
        if is_sync_running(sid):
            continue
        try:
            start_sync(sid)
            log.info("enqueued interval-sync for subscription %s", sid)
        except Exception:  # noqa: BLE001 - one bad subscription must not kill the loop
            log.exception("failed to enqueue sync for subscription %s", sid)


def _loop() -> None:
    tick = max(5, int(settings.sync_tick_seconds or 60))
    while not _stop.wait(tick):
        try:
            _tick()
        except Exception:  # noqa: BLE001 - keep the scheduler alive across errors
            log.exception("scheduler tick failed")


def start_scheduler() -> None:
    """Start the background scheduler (no-op if disabled or already running)."""
    global _thread
    if not settings.sync_enabled:
        log.info("interval-sync scheduler disabled (SYNC_ENABLED=false)")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="sync-scheduler", daemon=True)
    _thread.start()
    log.info("interval-sync scheduler started (tick=%ss)", settings.sync_tick_seconds)


def stop_scheduler() -> None:
    """Signal the scheduler loop to exit (called on shutdown)."""
    _stop.set()
