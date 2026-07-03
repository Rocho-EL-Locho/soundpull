"""Download page: form (URL prefillable via ?url=) + live progress cards."""
from __future__ import annotations

from datetime import datetime, timezone

from nicegui import ui

from app.auth import get_current_user
from app.db import session_scope
from app.fix_music_tags import TagOptions
from app.genres import DEFAULT_GENRE
from app.i18n import audio_format_labels, genre_options, t
from app.jobs import JobState, get_user_jobs, start_job, tag_options_from_settings
from app.pipeline import is_supported_url, normalize_audio_format
from app.theme import tag_option_switches


def _phase_order(js: JobState) -> list[str]:
    order = ["metadata", "download", "tags"]
    order.append("upload" if js.destination_type == "webdav" else "packaging")
    order.append("done")
    return order


def _job_card(js: JobState, delivered: set[str]) -> None:
    order = _phase_order(js)
    cur_idx = order.index(js.phase) if js.phase in order else -1

    with ui.card().classes("glass w-full rounded-xl p-4 gap-2"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(js.album or "…").classes("font-semibold truncate")
                ui.label(js.artist or "…").classes("text-sm text-white/60 truncate")
            ui.label(js.genre or t("genre.none")).classes("text-xs px-2 py-0.5 rounded-full glass text-white/70")

        if js.phase == "error":
            ui.icon("error").classes("text-red-400")
        else:
            with ui.row().classes("items-center gap-1 flex-wrap"):
                for i, p in enumerate(order):
                    if i < cur_idx or (js.phase == "done" and p == "done"):
                        icon, color = "check_circle", "text-emerald-400"
                    elif i == cur_idx:
                        icon, color = "radio_button_checked", "accent-text"
                    else:
                        icon, color = "radio_button_unchecked", "text-white/25"
                    with ui.row().classes("items-center gap-1"):
                        ui.icon(icon).classes(f"{color} text-base")
                        text_color = "text-white/80" if i <= cur_idx else "text-white/40"
                        ui.label(t(f"phase.{p}")).classes(f"text-xs {text_color}")
                    if i < len(order) - 1:
                        ui.element("div").classes("w-4 h-px bg-white/15")

        # Artist run (issue #32): show album i/N above the within-album track progress.
        if js.mode == "artist" and js.phase == "download" and js.total_albums:
            ui.linear_progress(value=js.current_album / js.total_albums, show_value=False) \
                .props("rounded color=accent").classes("w-full")
            ui.label(t("index.album_progress", current=js.current_album, total=js.total_albums)) \
                .classes("text-xs text-white/60")

        if js.phase == "download" and js.total_tracks:
            ui.linear_progress(value=js.current_track / js.total_tracks, show_value=False) \
                .props("rounded color=primary").classes("w-full")
            ui.label(t("index.track", current=js.current_track, total=js.total_tracks)) \
                .classes("text-xs text-white/60")

        if js.phase == "error":
            ui.label(js.error or t("index.unknown_error")).classes("text-red-400 text-sm")
        elif js.phase == "done":
            with ui.row().classes("items-center gap-3"):
                ui.label(t("index.completed")).classes("text-emerald-400 text-sm")
                if js.result_path:
                    ui.button(t("index.download_zip"), icon="download",
                              on_click=lambda p=js.result_path, n=js.result_name: ui.download.file(p, n)) \
                        .props("unelevated dense").classes("accent-grad text-white")
                elif js.destination_type == "webdav" and js.summary:
                    ui.label(js.summary).classes("text-xs text-white/50")
            if js.warning:  # upload OK but index update failed → surface the re-download risk (#38)
                ui.label(t(js.warning)).classes("text-amber-400 text-xs")  # warning is an i18n key
            # Auto-start the browser download once, only for a just-finished job.
            if js.result_path and js.id not in delivered and js.finished_at:
                age = (datetime.now(timezone.utc) - js.finished_at).total_seconds()
                if age < 20:
                    delivered.add(js.id)
                    ui.download.file(js.result_path, js.result_name)


def _field_label(text: str):
    return ui.label(text).classes("text-xs text-white/50")


def index_content(url: str = "") -> None:
    """Sub-page builder (mounted by the app-shell ``ui.sub_pages`` router)."""
    with session_scope() as session:
        user = get_current_user(session)
        if user is None:
            ui.navigate.to("/login")
            return
        uid = user.id
        us = user.settings
        d_genre = us.default_genre if us else DEFAULT_GENRE
        d_mode = us.default_mode if us else "album"
        d_audio = normalize_audio_format(us.default_audio_format if us else None)
        d_tags = tag_options_from_settings(us)
        d_dest = us.destination_type if us else "browser"
        if d_dest not in ("browser", "webdav"):
            d_dest = "browser"
        has_webdav = bool(us and us.webdav_url)
        d_dedup = bool(us and us.dedup_skip_existing)
        d_lyrics = bool(us and us.fetch_synced_lyrics)

    delivered: set[str] = set()  # browser downloads already auto-started

    @ui.refreshable
    def render_jobs() -> None:
        jobs = get_user_jobs(uid)
        if not jobs:
            ui.label(t("index.no_active")).classes("text-white/40 text-sm")
            return
        for js in jobs:
            _job_card(js, delivered)

    # Page heading: icon + title + subtitle, sitting above the form card (per design).
    with ui.row().classes("items-center gap-3"):
        ui.icon("download", size="30px").classes("accent-text")
        ui.label(t("nav.download")).classes("text-3xl font-bold text-white")
    ui.label(t("index.subtitle")).classes("text-white/50 text-sm")

    with ui.card().classes("glass w-full rounded-2xl p-7 gap-5"):
        # URL — external label + link-prefixed input (no floating label).
        with ui.column().classes("w-full gap-1.5"):
            _field_label(t("index.url_label"))
            url_in = ui.input(value=url, placeholder="https://music.youtube.com/...") \
                .props("outlined dense dark").classes("w-full")
            with url_in.add_slot("prepend"):
                ui.icon("link").classes("text-white/40")

        # Genre + audio format, side by side.
        with ui.row().classes("w-full gap-4 items-start"):
            with ui.column().classes("gap-1.5 flex-1 min-w-32"):
                _field_label(t("index.genre_label"))
                genre_sel = ui.select(genre_options(), value=d_genre) \
                    .props("outlined dense dark").classes("w-full")
            with ui.column().classes("gap-1.5 flex-1 min-w-32"):
                _field_label(t("index.audio_label"))
                audio_sel = ui.select(audio_format_labels(), value=d_audio) \
                    .props("outlined dense dark").classes("w-full")

        # Mode.
        with ui.column().classes("w-full gap-1.5"):
            _field_label(t("index.mode_label"))
            mode_tgl = ui.toggle({"album": t("common.album"), "single": t("common.single"),
                                  "playlist": t("common.playlist"),
                                  "artist": t("common.artist")}, value=d_mode) \
                .props("toggle-color=primary unelevated no-caps spread") \
                .classes("glass rounded-lg w-full")

        # Destination as two selectable cards (issue #31 dedup UI folds in below).
        dest_state = {"value": d_dest}
        dest_cards: dict[str, object] = {}

        def _select_dest(value: str) -> None:
            dest_state["value"] = value
            for v, card in dest_cards.items():
                (card.classes(add="sp-dest-card-active") if v == value
                 else card.classes(remove="sp-dest-card-active"))
            dedup_row.set_visibility(value == "webdav")
            _sync_dedup_for_mode()

        def _dest_card(value: str, icon: str, title: str, sub: str) -> None:
            card = ui.element("div").classes("sp-dest-card") \
                .on("click", lambda v=value: _select_dest(v))
            with card:
                ui.icon(icon, size="26px").classes("text-white/70")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(title).classes("sp-dest-title truncate")
                    ui.label(sub).classes("text-xs text-white/50 truncate")
            dest_cards[value] = card

        with ui.column().classes("w-full gap-1.5"):
            _field_label(t("index.dest_label"))
            with ui.row().classes("w-full gap-4"):
                _dest_card("browser", "folder", t("dest.browser_title"), t("dest.browser_sub"))
                _dest_card("webdav", "cloud_upload", t("dest.webdav"),
                           t("dest.webdav_sub") if has_webdav else t("dest.webdav_sub_unconfigured"))

        # Dedup (issue #31): only meaningful for WebDAV — shown only when WebDAV is chosen.
        with ui.row().classes("w-full items-center gap-2") as dedup_row:
            dedup_sw = ui.switch(t("index.dedup_label"), value=d_dedup) \
                .props("dense color=primary").classes("text-sm")

        def _sync_dedup_for_mode() -> None:
            # Artist runs always auto-dedup on WebDAV → lock the toggle on so the UI doesn't
            # imply you can turn skipping off for a whole-discography re-download.
            if mode_tgl.value == "artist" and dest_state["value"] == "webdav":
                dedup_sw.set_value(True)
                dedup_sw.disable()
            else:
                dedup_sw.enable()

        mode_tgl.on_value_change(lambda: _sync_dedup_for_mode())
        _select_dest(d_dest)  # sets initial card highlight + dedup visibility + toggle lock

        with ui.expansion(t("meta.heading"), icon="tune").classes("w-full glass rounded-lg") \
                .props("dense"):
            with ui.column().classes("w-full gap-1 p-2"):
                tag_switches = tag_option_switches(d_tags)
                # Synced lyrics (issue #43): fetch `.lrc` sidecars from LRCLIB; both dests.
                lyrics_sw = ui.switch(t("index.lyrics_label"), value=d_lyrics) \
                    .props("dense color=primary").classes("text-sm")

        def start() -> None:
            target = (url_in.value or "").strip()
            if not target:
                ui.notify(t("index.notify_need_url"), type="warning")
                return
            if not is_supported_url(target):
                ui.notify(t("index.notify_bad_url"), type="warning")
                return
            try:
                chosen_tags = TagOptions(**{f: bool(sw.value) for f, sw in tag_switches.items()})
                # Dedup only applies to WebDAV (browser ZIP has no library).
                dedup = bool(dedup_sw.value) and dest_state["value"] == "webdav"
                start_job(user_id=uid, url=target, genre=genre_sel.value,
                          mode=mode_tgl.value, destination_type=dest_state["value"],
                          audio_format=audio_sel.value, tag_options=chosen_tags, dedup=dedup,
                          fetch_lyrics=bool(lyrics_sw.value))
                ui.notify(t("index.notify_started"), type="positive")
                url_in.value = ""
                render_jobs.refresh()
            except Exception as exc:  # noqa: BLE001 - show config/validation errors
                ui.notify(str(exc), type="negative")

        ui.button(t("index.start_button"), icon="download", on_click=start) \
            .props("unelevated").classes("accent-grad text-white hover-glow self-end px-6")

    ui.label(t("index.active_heading")).classes("text-xs uppercase tracking-widest text-white/50 mt-2")
    render_jobs()
    ui.timer(1.0, render_jobs.refresh)
