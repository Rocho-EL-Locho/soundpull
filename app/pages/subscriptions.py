"""Subscriptions page: manage playlist interval-syncs (issue #21)."""
from __future__ import annotations

from nicegui import ui
from sqlmodel import select

from app.auth import get_current_user
from app.db import session_scope
from app.genres import DEFAULT_GENRE
from app.i18n import audio_format_labels, genre_options, t
from app.jobs import running_sync_phase, start_sync
from app.models import PlaylistSubscription, UserSettings
from app.pipeline import is_supported_url, normalize_audio_format
from app.theme import frame

# Interval select: option key → hours. Single source of truth for the dropdown.
_INTERVALS = {"6h": 6, "12h": 12, "daily": 24, "weekly": 168}


def _interval_options() -> dict[str, str]:
    return {key: t(f"subs.interval_{key}") for key in _INTERVALS}


def _interval_label(hours: int) -> str:
    for key, h in _INTERVALS.items():
        if h == hours:
            return t(f"subs.interval_{key}")
    return t("subs.every_hours", hours=hours)


def _status_label(sub: PlaylistSubscription) -> str:
    return {"ok": t("subs.status_ok"), "error": t("subs.status_error")}.get(
        sub.last_status, t("subs.status_idle"))


def _last_sync_text(sub: PlaylistSubscription) -> str:
    if sub.last_synced_at is None:
        return t("subs.last_sync_never")
    when = sub.last_synced_at.strftime("%Y-%m-%d %H:%M")
    return t("subs.last_sync", when=when, count=sub.last_new_count)


@ui.page("/subscriptions")
def subscriptions_page() -> None:
    with frame("subscriptions"):
        with session_scope() as session:
            user = get_current_user(session)
            if user is None:
                ui.navigate.to("/login")
                return
            uid = user.id
            us = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
            has_webdav = bool(us and us.webdav_url)
            d_genre = us.default_genre if us else DEFAULT_GENRE
            d_audio = normalize_audio_format(us.default_audio_format if us else None)

        @ui.refreshable
        def render_list() -> None:
            with session_scope() as session:
                subs = session.exec(
                    select(PlaylistSubscription)
                    .where(PlaylistSubscription.user_id == uid)
                    .order_by(PlaylistSubscription.created_at.desc())
                ).all()
                # Detach a lightweight snapshot so the cards don't touch a closed session.
                # `running_phase` reflects an in-flight sync (live), overriding last_status.
                rows = [(s.id, s.name, s.url, s.enabled,
                         _interval_label(s.interval_hours), _status_label(s), _last_sync_text(s),
                         s.last_status, s.last_error, running_sync_phase(s.id)) for s in subs]
            if not rows:
                ui.label(t("subs.empty")).classes("text-white/40 text-sm")
                return
            for row in rows:
                _sub_card(*row)

        def _sub_card(sid, name, url, enabled, interval_lbl, status_lbl,
                      last_txt, status, error, running_phase) -> None:
            with ui.card().classes("glass w-full rounded-xl p-4 gap-2"):
                with ui.row().classes("w-full items-start justify-between"):
                    with ui.column().classes("gap-0 min-w-0"):
                        ui.label(name or "Playlist").classes("font-semibold truncate")
                        ui.label(url).classes("text-xs text-white/50 truncate")
                    ui.switch(t("subs.enabled"), value=enabled,
                              on_change=lambda e, i=sid: _toggle(i, e.value)) \
                        .props("dense color=primary").classes("text-xs")
                with ui.row().classes("items-center gap-3 flex-wrap text-xs text-white/60"):
                    ui.label(interval_lbl).classes("px-2 py-0.5 rounded-full glass")
                    if running_phase:
                        # A sync is in flight: show the live phase instead of a stale status.
                        with ui.row().classes("items-center gap-1 text-cyan-300"):
                            ui.spinner(size="1.2em", color="primary")
                            ui.label(f"{t('subs.status_running')} · {t(f'phase.{running_phase}')}")
                    else:
                        color = "text-emerald-400" if status == "ok" else \
                            ("text-red-400" if status == "error" else "text-white/60")
                        ui.label(status_lbl).classes(color)
                    ui.label(last_txt)
                if not running_phase and status == "error" and error:
                    # `error` may be an i18n key (e.g. a failed seed) or a raw message; `t()`
                    # translates the former and returns the latter unchanged (issue #38).
                    ui.label(t(error)).classes("text-red-400 text-xs")
                with ui.row().classes("items-center gap-2"):
                    ui.button(t("subs.sync_now"), icon="sync",
                              on_click=lambda i=sid: _sync_now(i)) \
                        .props("unelevated dense").classes("accent-grad text-white")
                    ui.button(t("subs.delete"), icon="delete",
                              on_click=lambda i=sid: _delete(i)) \
                        .props("flat dense").classes("text-white/70")

        def _toggle(sid: int, value: bool) -> None:
            with session_scope() as session:
                sub = session.get(PlaylistSubscription, sid)
                if sub and sub.user_id == uid:
                    sub.enabled = value
                    session.add(sub)

        def _sync_now(sid: int) -> None:
            start_sync(sid)
            ui.notify(t("subs.notify_sync_started"), type="positive")
            render_list.refresh()

        def _delete(sid: int) -> None:
            with session_scope() as session:
                sub = session.get(PlaylistSubscription, sid)
                if sub and sub.user_id == uid:
                    session.delete(sub)
            ui.notify(t("subs.notify_deleted"), type="positive")
            render_list.refresh()

        with ui.card().classes("glass w-full rounded-2xl p-6 gap-4"):
            ui.label(t("subs.heading_new")).classes("text-xl font-semibold accent-text")
            ui.label(t("subs.desc")).classes("text-xs text-white/50")
            if not has_webdav:
                ui.label(t("subs.no_webdav")).classes("text-amber-300 text-sm")

            url_in = ui.input(t("subs.url_label"),
                              placeholder="https://music.youtube.com/playlist?list=...") \
                .props("outlined dense dark").classes("w-full")
            with ui.row().classes("w-full gap-3 items-end"):
                interval_sel = ui.select(_interval_options(), value="daily",
                                         label=t("subs.interval_label")) \
                    .props("outlined dense dark").classes("flex-1 min-w-32")
                genre_sel = ui.select(genre_options(), value=d_genre, label=t("index.genre_label")) \
                    .props("outlined dense dark").classes("flex-1 min-w-32")
            audio_sel = ui.select(audio_format_labels(), value=d_audio, label=t("index.audio_label")) \
                .props("outlined dense dark").classes("w-full")
            with ui.column().classes("gap-1"):
                ui.label(t("subs.initial_label")).classes("text-xs text-white/50")
                initial_tgl = ui.toggle(
                    {"download_all": t("subs.initial_download_all"),
                     "mark_existing": t("subs.initial_mark_existing")},
                    value="download_all") \
                    .props("toggle-color=primary unelevated no-caps").classes("glass rounded-lg")

            def create() -> None:
                if not has_webdav:
                    ui.notify(t("subs.no_webdav"), type="warning")
                    return
                target = (url_in.value or "").strip()
                if not target:
                    ui.notify(t("subs.notify_need_url"), type="warning")
                    return
                if not is_supported_url(target):
                    ui.notify(t("subs.notify_bad_url"), type="warning")
                    return
                with session_scope() as session:
                    session.add(PlaylistSubscription(
                        user_id=uid, url=target, name="Playlist",
                        interval_hours=_INTERVALS.get(interval_sel.value, 24),
                        genre=genre_sel.value,
                        audio_format=normalize_audio_format(audio_sel.value),
                        initial_mode=initial_tgl.value,
                    ))
                ui.notify(t("subs.notify_created"), type="positive")
                url_in.value = ""
                render_list.refresh()

            ui.button(t("subs.create_button"), icon="add", on_click=create) \
                .props("unelevated").classes("accent-grad text-white hover-glow self-end px-6")

        ui.label(t("subs.list_heading")).classes("text-xs uppercase tracking-widest text-white/50 mt-2")
        render_list()
        # Refresh so status/last-sync update as the scheduler/worker progresses.
        ui.timer(3.0, render_list.refresh)
