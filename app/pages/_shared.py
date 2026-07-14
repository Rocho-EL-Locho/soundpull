"""Shared UI helpers for the library-management pages (roadmap 03).

`run_library_task` factors out the identical shape of every off-thread WebDAV maintenance
action — server scan, lyrics backfill, folder/track trash: *notify "running" → run the
blocking call via `run.io_bound` → surface a config/connection error → otherwise turn the
result into a done/incomplete notification*. Both the settings page and the library page use
it so the behaviour (and the "scanned Nh ago" stamp written by `scan_webdav`) stays in one
place.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from nicegui import run, ui

from app.i18n import t

log = logging.getLogger("pages")


async def run_library_task(
    fn: Callable[[], Any],
    *,
    running_key: str,
    error_key: str,
    done: Callable[[Any], tuple[str, str]],
) -> Any:
    """Run a blocking library task off-thread with the standard notify lifecycle.

    - ``fn`` — a zero-arg callable executed via `run.io_bound` (wrap the real call in a closure
      so any pre-computed args/`uid` are bound).
    - ``running_key`` — i18n key for the "ongoing" toast.
    - ``error_key`` — i18n key for the failure toast; called as ``t(error_key, error=exc)``.
    - ``done(result) -> (notify_type, message)`` — maps a successful result to the final toast
      (e.g. ``("positive", …)`` or ``("warning", …)`` for an incomplete run).

    Returns the task's result, or ``None`` if it raised (the error toast has already fired).
    """
    ui.notify(t(running_key), type="ongoing")
    try:
        result = await run.io_bound(fn)
    except Exception as exc:  # noqa: BLE001 - surface config/connection errors to the user
        ui.notify(t(error_key, error=exc), type="negative")
        return None
    notify_type, message = done(result)
    ui.notify(message, type=notify_type)
    return result
