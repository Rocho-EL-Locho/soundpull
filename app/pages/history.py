"""History page: the current user's past downloads (durable, from the DB).

Interactive (issue #44): text/mode/destination/status/date filtering, per-row
retry (re-queue via ``start_job``), delete (with a confirm dialog), and a detail
dialog showing full metadata plus the job's event timeline (``DownloadHistory.log``).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlmodel import select

from nicegui import ui

from app.auth import get_current_user
from app.db import session_scope
from app.i18n import t
from app.jobs import start_batch, start_job
from app.models import DownloadHistory, UserSettings
from app.pipeline import audio_format_short
from app.theme import ghost_button, primary_button

log = logging.getLogger("pages.history")

# phase → (translation key, color); label resolved per request via t().
_STATUS = {
    "done": ("history.status_done", "text-emerald-400"),
    "error": ("history.status_error", "text-red-400"),
    "queued": ("history.status_queued", "text-white/60"),
    "metadata": ("history.status_running", "text-cyan-300"),
    "download": ("history.status_running", "text-cyan-300"),
    "tags": ("history.status_running", "text-cyan-300"),
    "upload": ("history.status_running", "text-cyan-300"),
}

# The "running" status filter groups every phase mapped to the running label — derived
# from _STATUS so a newly-added running phase is picked up here in one place.
_RUNNING_PHASES = tuple(p for p, (k, _c) in _STATUS.items() if k == "history.status_running")

# Retry/delete are offered only on a finished job (a still-running/queued one is off limits).
_TERMINAL_PHASES = ("done", "error")


def _parse_date(value: str) -> datetime | None:
    """Parse an HTML date-input value (``YYYY-MM-DD``); None if empty/malformed."""
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def build_history_query(user_id: int, *, search: str = "", mode: str = "", dest: str = "",
                        status: str = "", date_from: str = "", date_to: str = ""):
    """Build the filtered ``DownloadHistory`` query for a user (newest first).

    A pure query builder (no NiceGUI) so it stays unit-testable. Every filter is
    optional; an empty value places no constraint on that field. Text search matches
    artist/album/url case-insensitively; ``status="running"`` groups all in-flight
    phases; the date bounds are inclusive of the whole ``date_to`` day.
    """
    stmt = select(DownloadHistory).where(DownloadHistory.user_id == user_id)
    query = (search or "").strip()
    if query:
        like = f"%{query}%"
        stmt = stmt.where(or_(
            DownloadHistory.artist.ilike(like),
            DownloadHistory.album.ilike(like),
            DownloadHistory.url.ilike(like),
        ))
    if mode:
        stmt = stmt.where(DownloadHistory.mode == mode)
    if dest:
        stmt = stmt.where(DownloadHistory.destination_type == dest)
    if status == "running":
        stmt = stmt.where(DownloadHistory.phase.in_(_RUNNING_PHASES))
    elif status:
        stmt = stmt.where(DownloadHistory.phase == status)
    start = _parse_date(date_from)
    if start:
        stmt = stmt.where(DownloadHistory.created_at >= start)
    end = _parse_date(date_to)
    if end:
        stmt = stmt.where(DownloadHistory.created_at < end + timedelta(days=1))
    return stmt.order_by(DownloadHistory.created_at.desc())


def retry_options(row: DownloadHistory, us: UserSettings | None) -> dict:
    """``start_job`` kwargs to re-run a history row (issue #44).

    URL/genre/mode/format/destination come straight from the stored row. The
    per-download toggles (tag_options/dedup/lyrics) were never persisted, so they
    fall back to the user's current settings — mirroring the download form's
    defaults: ``tag_options=None`` lets ``start_job`` fill them from settings;
    dedup follows the saved default (WebDAV only) and is forced on for an artist
    run; lyrics follow the saved default.
    """
    dedup = row.mode == "artist" or (
        bool(us and us.dedup_skip_existing) and row.destination_type == "webdav")
    return {
        "url": row.url, "genre": row.genre, "mode": row.mode,
        "audio_format": row.audio_format, "destination_type": row.destination_type,
        "tag_options": None, "dedup": dedup,
        "fetch_lyrics": bool(us and us.fetch_synced_lyrics),
    }


def history_content() -> None:
    """Sub-page builder (mounted by the app-shell ``ui.sub_pages`` router)."""
    with session_scope() as session:
        user = get_current_user(session)
        if user is None:
            ui.navigate.to("/login")
            return
        uid = user.id

    # Filter state, read by the refreshable list and mutated by the inputs above it.
    flt = {"search": "", "mode": "", "dest": "", "status": "", "date_from": "", "date_to": ""}

    def _set(**kw) -> None:
        flt.update(**kw)
        render_list.refresh()

    @ui.refreshable
    def render_list() -> None:
        with session_scope() as session:
            rows = session.exec(build_history_query(
                uid, search=flt["search"], mode=flt["mode"], dest=flt["dest"],
                status=flt["status"], date_from=flt["date_from"], date_to=flt["date_to"],
            )).all()
            # Detach a plain-dict snapshot so cards/dialogs don't touch a closed session.
            items = [{
                "id": r.id, "album": r.album, "artist": r.artist, "phase": r.phase,
                "mode": r.mode, "genre": r.genre, "dest": r.destination_type, "url": r.url,
                "audio": audio_format_short(r.audio_format),
                "created": r.created_at, "finished": r.finished_at,
                "error": r.error, "warning": r.warning, "log": r.log,
                "current": r.current_track, "total": r.total_tracks, "failed": r.failed_tracks,
            } for r in rows]

        if not items:
            # Distinguish "nothing downloaded yet" from "filters matched nothing".
            key = "history.no_results" if any(flt.values()) else "history.empty"
            ui.label(t(key)).classes("text-white/40 text-sm")
            return
        for it in items:
            _history_card(it)

    def _history_card(it: dict) -> None:
        key, color = _STATUS.get(it["phase"], ("history.status_unknown", "text-white/60"))
        with ui.card().classes("glass w-full rounded-xl p-4 gap-1"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(it["album"] or "—").classes("font-semibold truncate")
                    ui.label(it["artist"] or "—").classes("text-sm text-white/60 truncate")
                ui.label(t(key)).classes(f"text-sm {color}")
            with ui.row().classes("items-center gap-3 text-xs text-white/45 flex-wrap"):
                ui.label(it["created"].strftime("%d.%m.%Y %H:%M"))
                genre = it["genre"] or t("genre.none")
                ui.label(f"{it['mode']} · {genre} · {it['audio']} · {it['dest']}")
            if it["error"]:
                ui.label(it["error"]).classes("text-red-400 text-xs")
            if it["warning"]:  # non-fatal note on a done job (index update failed #38, or partial)
                # `warning` is stored as an i18n key by the worker; resolve it here where the
                # request has a language. The partial-delivery note carries counts ("N von M");
                # `t()` fills the slots and returns unknown strings unchanged.
                ui.label(t(it["warning"], failed=it["failed"], total=it["total"])).classes(
                    "text-amber-400 text-xs")
            with ui.row().classes("items-center gap-2 pt-1"):
                ghost_button(t("history.action_details"), icon="info",
                             on_click=lambda d=it: _details(d)).props("dense")
                # Retry/delete only on a finished job: retrying a running one starts a duplicate
                # download, and deleting it drops the row its worker is still writing to.
                if it["phase"] in _TERMINAL_PHASES:
                    primary_button(t("history.action_retry"), icon="replay",
                                   on_click=lambda i=it["id"]: _retry(i)).props("dense")
                    ghost_button(t("history.action_delete"), icon="delete",
                                 on_click=lambda i=it["id"]: _delete(i)).props("dense")

    def _retry(rid: str) -> None:
        with session_scope() as session:
            row = session.get(DownloadHistory, rid)
            if row is None or row.user_id != uid:
                return
            us = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
            # A batch (roadmap 12) has no single re-runnable URL — re-run the stored item list via
            # start_batch. Dedup skips already-present tracks on WebDAV, so a retry only re-pulls
            # what's missing. Everything else re-runs through start_job with the row's scalars.
            # A corrupt `batch_urls` (tampered/legacy row) must not crash the click — treat as empty.
            batch_items = None
            if row.mode == "batch":
                try:
                    batch_items = json.loads(row.batch_urls) if row.batch_urls else []
                except (ValueError, TypeError):
                    batch_items = []
            opts = retry_options(row, us)  # plain scalars → safe after the session closes
        try:
            if batch_items is not None:
                if not batch_items:
                    ui.notify(t("history.notify_retry_failed"), type="negative")
                    return
                start_batch(user_id=uid, items=batch_items, genre=opts["genre"],
                            destination_type=opts["destination_type"],
                            audio_format=opts["audio_format"], dedup=opts["dedup"],
                            fetch_lyrics=opts["fetch_lyrics"])
            else:
                start_job(user_id=uid, **opts)
        except ValueError:  # e.g. WebDAV destination but no target configured
            ui.notify(t("history.notify_retry_no_dest"), type="warning")
            return
        except Exception:  # noqa: BLE001 - decrypt (rotated FERNET_KEY) / DB error, etc.
            log.exception("retry of history job %s failed", rid)
            ui.notify(t("history.notify_retry_failed"), type="negative")
            return
        ui.notify(t("history.notify_retry_started"), type="positive")
        render_list.refresh()

    def _delete(rid: str) -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("glass w-full max-w-sm rounded-2xl p-4 gap-2"):
            ui.label(t("history.confirm_delete_heading")).classes("font-semibold")
            ui.label(t("history.confirm_delete_text")).classes("text-sm text-white/70")

            def confirm() -> None:
                with session_scope() as session:
                    row = session.get(DownloadHistory, rid)
                    if row and row.user_id == uid:
                        session.delete(row)
                dialog.close()
                ui.notify(t("history.notify_deleted"), type="positive")
                render_list.refresh()

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ghost_button(t("settings.cancel"), on_click=dialog.close)
                primary_button(t("history.confirm_delete_yes"), icon="delete", on_click=confirm)
        dialog.open()

    def _details(it: dict) -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("glass w-full max-w-lg rounded-2xl p-4 gap-2"):
            ui.label(t("history.detail_heading")).classes("text-lg font-semibold accent-text")
            key, _color = _STATUS.get(it["phase"], ("history.status_unknown", "text-white/60"))

            def line(label_key: str, value, value_class: str = "text-white/90") -> None:
                if value in (None, ""):
                    return
                with ui.row().classes("w-full gap-2 text-sm items-start"):
                    ui.label(t(label_key)).classes("text-white/50 min-w-28 shrink-0")
                    ui.label(str(value)).classes(f"{value_class} break-all")

            line("history.detail_status", t(key))
            line("history.detail_url", it["url"])
            line("history.detail_mode", it["mode"])
            line("history.detail_dest", it["dest"])
            line("history.detail_audio", it["audio"])
            line("history.detail_genre", it["genre"])
            line("history.detail_artist", it["artist"])
            line("history.detail_album", it["album"])
            line("history.detail_created", it["created"].strftime("%d.%m.%Y %H:%M:%S"))
            if it["finished"]:
                line("history.detail_finished", it["finished"].strftime("%d.%m.%Y %H:%M:%S"))
            if it["total"]:
                line("history.detail_tracks", f"{it['current']} / {it['total']}")
            if it["failed"]:
                line("history.detail_failed", it["failed"], "text-amber-400")
            line("history.detail_error", it["error"], "text-red-400")
            if it["warning"]:
                line("history.detail_warning",
                     t(it["warning"], failed=it["failed"], total=it["total"]), "text-amber-400")

            ui.separator()
            ui.label(t("history.detail_log")).classes("text-white/50 text-sm")
            if it["log"]:
                ui.label(it["log"]).classes(
                    "whitespace-pre-wrap font-mono text-xs text-white/80 w-full "
                    "max-h-60 overflow-auto glass rounded-lg p-2")
            else:
                ui.label(t("history.detail_no_log")).classes("text-white/40 text-xs")
            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ghost_button(t("settings.cancel"), on_click=dialog.close)
        dialog.open()

    ui.label(t("history.heading")).classes("text-xl font-semibold accent-text")

    with ui.card().classes("glass w-full rounded-xl p-4 gap-3"):
        ui.input(t("history.filter_search"),
                 on_change=lambda e: _set(search=e.value or "")) \
            .props("outlined dense dark clearable debounce=300").classes("w-full")
        with ui.row().classes("w-full gap-3 items-end flex-wrap"):
            _all = t("history.filter_all")
            ui.select({"": _all, "album": t("common.album"), "single": t("common.single"),
                       "playlist": t("common.playlist"), "artist": t("common.artist"),
                       "batch": t("common.batch")},
                      value="", label=t("index.mode_label"),
                      on_change=lambda e: _set(mode=e.value)) \
                .props("outlined dense dark").classes("flex-1 min-w-32")
            ui.select({"": _all, "browser": t("dest.browser_title"), "webdav": t("dest.webdav")},
                      value="", label=t("index.dest_label"),
                      on_change=lambda e: _set(dest=e.value)) \
                .props("outlined dense dark").classes("flex-1 min-w-32")
            ui.select({"": _all, "done": t("history.status_done"),
                       "running": t("history.status_running"),
                       "error": t("history.status_error"), "queued": t("history.status_queued")},
                      value="", label=t("history.filter_status"),
                      on_change=lambda e: _set(status=e.value)) \
                .props("outlined dense dark").classes("flex-1 min-w-32")
        with ui.row().classes("w-full gap-3 items-end flex-wrap"):
            ui.input(t("history.filter_from"),
                     on_change=lambda e: _set(date_from=e.value or "")) \
                .props("outlined dense dark type=date").classes("flex-1 min-w-40")
            ui.input(t("history.filter_to"),
                     on_change=lambda e: _set(date_to=e.value or "")) \
                .props("outlined dense dark type=date").classes("flex-1 min-w-40")

    render_list()
