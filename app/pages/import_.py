"""Batch import page (roadmap 12): paste a track list → match → review → download as one job.

Paste ``Artist - Title`` lines (or a simple CSV), match each against YouTube Music
(`app.matching`, which composes feature 07's search), review the matches in a table (pre-checked by
confidence, library-duplicates pre-unchecked), then download the confirmed rows as ONE batch job
(`jobs.start_batch`). Matching runs in a background registry polled by `ui.timer` — the UI never
blocks and leaving the page cancels. No pipeline/tag code here.
"""
from __future__ import annotations

import logging

from nicegui import ui
from sqlmodel import select

from app import jobs, matching
from app.auth import get_current_user
from app.db import session_scope
from app.genres import DEFAULT_GENRE
from app.i18n import audio_format_labels, genre_options, t
from app.jobs import tag_options_from_settings
from app.models import UserSettings
from app.pipeline import normalize_audio_format

log = logging.getLogger("import_page")

_PHASE_KEYS = {"queued": "import.phase_queued", "matching": "import.phase_matching"}


def _confidence_badge(m: matching.Match) -> None:
    if m.best is None:
        ui.badge(t("import.unmatched")).props("color=negative")
    elif m.confidence >= matching.HIGH_CONFIDENCE:
        ui.badge(f"{round(m.confidence * 100)}%").props("color=positive")
    else:
        ui.badge(f"{round(m.confidence * 100)}%").props("color=warning")


def import_content() -> None:
    """Sub-page builder (mounted by the app-shell ``ui.sub_pages`` router)."""
    with session_scope() as session:
        user = get_current_user(session)
        if user is None:
            ui.navigate.to("/login")
            return
        uid = user.id
        us = user.settings
        d_genre = us.default_genre if us else DEFAULT_GENRE
        d_audio = normalize_audio_format(us.default_audio_format if us else None)
        d_dest = us.destination_type if us and us.destination_type in ("browser", "webdav") \
            else "browser"
        d_dedup = bool(us and us.dedup_skip_existing)
        d_lyrics = bool(us and us.fetch_synced_lyrics)

    # `matches` holds the finished match run; `selected`/`choice` are keyed by the stable row id
    # (the match's index), NOT list position of a filtered view.
    state: dict = {"matches": None, "last_finished": True, "selected": {}, "choice": {}}

    # --- match lifecycle -----------------------------------------------------------------
    def _start_match() -> None:
        text = paste.value or ""
        non_empty = [ln for ln in text.splitlines() if ln.strip()]
        if len(non_empty) > matching.MAX_LINES:
            ui.notify(t("import.too_many_lines", max=matching.MAX_LINES), type="warning")
            return
        if not any(p.ok for p in matching.parse_lines(text)):
            ui.notify(t("import.nothing_to_match"), type="warning")
            return
        if not matching.start_match(uid, text):
            ui.notify(t("import.busy"), type="warning")
            return
        state["matches"] = None
        state["last_finished"] = False
        render_body.refresh()

    def _poll() -> None:
        st = matching.get_match_state(uid)
        if st is None:
            return
        if not st.finished:
            render_body.refresh()
            return
        if not state["last_finished"]:
            state["last_finished"] = True
            if st.error:
                ui.notify(t("import.error", error=st.error), type="negative")
            else:
                state["matches"] = st.matches
                sel: dict = {}
                choice: dict = {}
                for i, m in enumerate(st.matches):
                    if m.best is not None:
                        choice[str(i)] = m.best.url
                        # Pre-check confident, matched, not-already-in-library rows.
                        sel[str(i)] = m.confidence >= matching.HIGH_CONFIDENCE and not m.on_server
                state["selected"], state["choice"] = sel, choice
            render_body.refresh()

    # --- download ------------------------------------------------------------------------
    async def _download() -> None:
        chosen = [state["choice"][rid] for rid, on in state["selected"].items()
                  if on and state["choice"].get(rid)]
        if not chosen:
            ui.notify(t("import.none_selected"), type="warning")
            return
        dialog = ui.dialog()
        with dialog, ui.card().classes("glass w-full max-w-sm rounded-2xl p-4 gap-2"):
            ui.label(t("import.confirm_title")).classes("font-semibold")
            ui.label(t("import.confirm_body", count=len(chosen))).classes("text-sm text-white/70")

            def confirm() -> None:
                dialog.close()
                try:
                    jobs.start_batch(user_id=uid, items=chosen, genre=genre_sel.value,
                                     destination_type=dest_sel.value, audio_format=audio_sel.value,
                                     dedup=bool(dedup_sw.value) and dest_sel.value == "webdav",
                                     fetch_lyrics=bool(lyrics_sw.value))
                except Exception as exc:  # noqa: BLE001 - surface config/validation errors
                    ui.notify(str(exc), type="negative")
                    return
                ui.notify(t("import.started", count=len(chosen)), type="positive")
                ui.navigate.to("/")   # the index page picks the new job up via its jobs poll

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button(t("settings.cancel"), on_click=dialog.close).props("flat")
                ui.button(t("import.download_button"), icon="download",
                          on_click=confirm).classes("accent-grad text-white")
        dialog.open()

    # --- review rows ---------------------------------------------------------------------
    def _match_row(rid: str, m: matching.Match) -> None:
        with ui.row().classes("w-full flex-nowrap items-center gap-3 py-1 border-t border-white/10"):
            ui.checkbox(value=state["selected"].get(rid, False),
                        on_change=lambda e, i=rid: state["selected"].__setitem__(i, e.value)) \
                .props("dense")
            if m.best and m.best.thumbnail:
                ui.image(m.best.thumbnail).classes("w-9 h-9 rounded object-cover shrink-0")
            with ui.column().classes("gap-0 min-w-0 flex-1"):
                ui.label(m.line.raw).classes("text-xs text-white/45 truncate")
                if len(m.candidates) > 1:
                    options = {c.url: f"{c.artist} – {c.title}" for c in m.candidates}
                    ui.select(options, value=state["choice"].get(rid),
                              on_change=lambda e, i=rid: state["choice"].__setitem__(i, e.value)) \
                        .props("outlined dense dark").classes("w-full text-sm")
                elif m.best:
                    ui.label(f"{m.best.artist} – {m.best.title}").classes(
                        "text-sm text-white/85 truncate")
            with ui.row().classes("items-center gap-1 shrink-0"):
                if m.on_server:
                    ui.badge(t("import.on_server")).props("color=grey")
                _confidence_badge(m)

    @ui.refreshable
    def render_body() -> None:
        st = matching.get_match_state(uid)
        if st is not None and not st.finished:
            with ui.card().classes("glass w-full rounded-2xl p-8 gap-3 items-center text-center"):
                ui.spinner(size="lg").classes("text-white/60")
                label = t(_PHASE_KEYS.get(st.phase, "import.phase_queued"))
                if st.phase == "matching" and st.total_count:
                    label += f" ({st.done_count}/{st.total_count})"
                ui.label(label).classes("text-white/70")
            return

        matches = state["matches"]
        if matches is None:
            return
        matched = [(str(i), m) for i, m in enumerate(matches) if m.best is not None]
        unmatched = [m for m in matches if m.best is None]

        if matched:
            with ui.card().classes("glass w-full rounded-2xl p-4 gap-1"):
                with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                    ui.label(t("import.matched_heading", count=len(matched))).classes(
                        "text-sm uppercase tracking-widest text-white/50")
                    ui.button(t("import.download_button"), icon="download", on_click=_download) \
                        .props("unelevated").classes("accent-grad text-white")
                for rid, m in matched:
                    _match_row(rid, m)

        if unmatched:
            with ui.expansion(t("import.unmatched_heading", count=len(unmatched))) \
                    .classes("w-full glass rounded-2xl").props("dense"):
                for m in unmatched:
                    reason = t(m.line.error) if m.line.error else t("import.no_result")
                    ui.label(f"{m.line.raw}  —  {reason}").classes(
                        "text-xs text-white/50 break-all py-0.5")

    # --- header + paste + options (outside the refreshable) ------------------------------
    with ui.card().classes("glass w-full rounded-2xl p-6 gap-4"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("playlist_add", size="26px").classes("accent-text")
            ui.label(t("import.heading")).classes("text-xl font-semibold accent-text")
        ui.label(t("import.intro")).classes("text-xs text-white/50")

        paste = ui.textarea(placeholder=t("import.placeholder")) \
            .props("outlined dense dark autogrow").classes("w-full font-mono text-sm")

        @ui.refreshable
        def render_count() -> None:
            parsed = matching.parse_lines(paste.value or "")
            ok = sum(1 for p in parsed if p.ok)
            bad = len(parsed) - ok
            if parsed:
                ui.label(t("import.parsed_count", ok=ok, skipped=bad)).classes(
                    "text-xs text-white/50")

        paste.on_value_change(lambda: render_count.refresh())
        render_count()

        # Download options (default from the user's saved settings).
        with ui.row().classes("w-full gap-4 items-start flex-wrap"):
            with ui.column().classes("gap-1.5 flex-1 min-w-32"):
                ui.label(t("index.genre_label")).classes("text-xs text-white/50")
                genre_sel = ui.select(genre_options(), value=d_genre) \
                    .props("outlined dense dark").classes("w-full")
            with ui.column().classes("gap-1.5 flex-1 min-w-32"):
                ui.label(t("index.audio_label")).classes("text-xs text-white/50")
                audio_sel = ui.select(audio_format_labels(), value=d_audio) \
                    .props("outlined dense dark").classes("w-full")
            with ui.column().classes("gap-1.5 flex-1 min-w-32"):
                ui.label(t("index.dest_label")).classes("text-xs text-white/50")
                dest_sel = ui.toggle({"browser": t("dest.browser_title"), "webdav": t("dest.webdav")},
                                     value=d_dest).props("toggle-color=primary no-caps dense") \
                    .classes("glass rounded-lg")
        with ui.row().classes("w-full items-center gap-4 flex-wrap"):
            dedup_sw = ui.switch(t("index.dedup_label"), value=d_dedup) \
                .props("dense color=primary").classes("text-sm")
            lyrics_sw = ui.switch(t("index.lyrics_label"), value=d_lyrics) \
                .props("dense color=primary").classes("text-sm")

        ui.button(t("import.match_button"), icon="search", on_click=_start_match) \
            .props("unelevated").classes("accent-grad text-white hover-glow self-start px-6")

    render_body()
    ui.timer(1.5, _poll)
