"""History page: the current user's past downloads (durable, from the DB)."""
from __future__ import annotations

from sqlmodel import select

from nicegui import ui

from app.auth import get_current_user
from app.db import session_scope
from app.models import DownloadHistory
from app.theme import frame

_STATUS = {
    "done": ("Fertig", "text-emerald-400"),
    "error": ("Fehler", "text-red-400"),
    "queued": ("Warteschlange", "text-white/60"),
    "metadata": ("Läuft", "text-cyan-300"),
    "download": ("Läuft", "text-cyan-300"),
    "tags": ("Läuft", "text-cyan-300"),
    "upload": ("Läuft", "text-cyan-300"),
}


@ui.page("/history")
def history_page() -> None:
    with frame("history"):
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
                "created": r.created_at, "error": r.error,
            } for r in rows]

        ui.label("Verlauf").classes("text-xl font-semibold accent-text")
        if not items:
            ui.label("Noch keine Downloads.").classes("text-white/40 text-sm")
            return

        for it in items:
            label, color = _STATUS.get(it["phase"], ("?", "text-white/60"))
            with ui.card().classes("glass w-full rounded-xl p-4 gap-1"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0 min-w-0"):
                        ui.label(it["album"] or "—").classes("font-semibold truncate")
                        ui.label(it["artist"] or "—").classes("text-sm text-white/60 truncate")
                    ui.label(label).classes(f"text-sm {color}")
                with ui.row().classes("items-center gap-3 text-xs text-white/45 flex-wrap"):
                    ui.label(it["created"].strftime("%d.%m.%Y %H:%M"))
                    ui.label(f"{it['mode']} · {it['genre']} · {it['dest']}")
                if it["error"]:
                    ui.label(it["error"]).classes("text-red-400 text-xs")
