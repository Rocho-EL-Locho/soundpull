"""History page: the current user's past downloads (durable, from the DB)."""
from __future__ import annotations

from sqlmodel import select

from nicegui import ui

from app.auth import get_current_user
from app.db import session_scope
from app.i18n import t
from app.models import DownloadHistory
from app.pipeline import audio_format_short

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


def history_content() -> None:
    """Sub-page builder (mounted by the app-shell ``ui.sub_pages`` router)."""
    with session_scope() as session:
        user = get_current_user(session)
        if user is None:
            ui.navigate.to("/login")
            return
        rows = session.exec(
            select(DownloadHistory)
            .where(DownloadHistory.user_id == user.id)
            .order_by(DownloadHistory.created_at.desc())
        ).all()
        items = [{
            "album": r.album, "artist": r.artist, "phase": r.phase, "mode": r.mode,
            "genre": r.genre, "dest": r.destination_type, "url": r.url,
            "audio": audio_format_short(r.audio_format),
            "created": r.created_at, "error": r.error, "warning": r.warning,
            "total": r.total_tracks, "failed": r.failed_tracks,
        } for r in rows]

    ui.label(t("history.heading")).classes("text-xl font-semibold accent-text")
    if not items:
        ui.label(t("history.empty")).classes("text-white/40 text-sm")
        return

    for it in items:
        key, color = _STATUS.get(it["phase"], ("history.status_unknown", "text-white/60"))
        label = t(key)
        with ui.card().classes("glass w-full rounded-xl p-4 gap-1"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(it["album"] or "—").classes("font-semibold truncate")
                    ui.label(it["artist"] or "—").classes("text-sm text-white/60 truncate")
                ui.label(label).classes(f"text-sm {color}")
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
