"""Duplicate finder & cleanup page (roadmap 04): review and resolve library duplicates.

Runs a background library analysis (`app.duplicates.start_analysis`) whose progress is polled
via `ui.timer`, then renders the persisted report: exact-key duplicate groups as cards with a
pre-selected keeper (`ui.radio`) and a per-group confirm dialog, plus an "accept all exact
suggestions" bulk action; probable (noise-variant) groups are shown collapsed and never bulk-
resolved. Resolving trashes the non-keepers (safe trash) and repairs playlist references. This
page never touches the download pipeline or the tag chain.
"""
from __future__ import annotations

import logging

from nicegui import run, ui
from sqlmodel import select

from app import duplicates
from app.auth import get_current_user
from app.db import session_scope
from app.i18n import t
from app.models import UserSettings

log = logging.getLogger("duplicates_page")

_PHASE_KEYS = {
    "queued": "duplicates.phase_queued",
    "scanning": "duplicates.phase_scanning",
    "grouping": "duplicates.phase_grouping",
}


def _folder_label(info) -> str:
    """Human 'playlist · N tracks' / 'album · N tracks' / 'single' tag for a copy."""
    if info.is_playlist_folder:
        kind = t("duplicates.kind_playlist")
    elif info.folder_track_count <= 1:
        kind = t("duplicates.kind_single")
    else:
        kind = t("duplicates.kind_album")
    return t("duplicates.folder_tag", kind=kind, count=info.folder_track_count)


def duplicates_content() -> None:
    """Sub-page builder (mounted by the app-shell ``ui.sub_pages`` router)."""
    with session_scope() as session:
        user = get_current_user(session)
        if user is None:
            ui.navigate.to("/login")
            return
        uid = user.id
        us = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
        has_webdav = bool(us and us.webdav_url)

    # `report` is the persisted analysis; `keepers[gid]` overrides the suggested keeper per group.
    state: dict = {"report": duplicates.load_report(uid), "keepers": {}, "last_finished": True}

    def _reload_report() -> None:
        state["report"] = duplicates.load_report(uid)
        state["keepers"] = {}

    # --- analysis lifecycle --------------------------------------------------------------
    def analyze() -> None:
        if not duplicates.start_analysis(uid):
            ui.notify(t("duplicates.busy"), type="warning")
            return
        state["last_finished"] = False
        render_body.refresh()

    def _poll() -> None:
        st = duplicates.get_analysis_state(uid)
        if st is None:
            return
        if not st.finished:
            render_body.refresh()  # cheap; only the status card is rendered while running
            return
        if not state["last_finished"]:
            state["last_finished"] = True
            if st.error:
                ui.notify(t("duplicates.error", error=st.error), type="negative")
            else:
                ui.notify(t("duplicates.done", exact=st.exact_count,
                            probable=st.probable_count), type="positive")
                _reload_report()
            render_body.refresh()

    # --- resolve actions -----------------------------------------------------------------
    def _keeper_for(gid: str, group) -> str:
        return state["keepers"].get(gid, group.suggested_keeper)

    async def _do_resolve(groups_with_ids: list[tuple[str, object]]) -> None:
        """Trash the non-keepers of each (gid, group) off-thread, then drop them from the view."""
        def _work() -> int:
            trashed = 0
            for gid, group in groups_with_ids:
                keeper = _keeper_for(gid, group)
                remove = [p.rel_path for p in group.paths if p.rel_path != keeper]
                res = duplicates.resolve_group(uid, keeper, remove)
                trashed += len(res.trashed)
            return trashed
        try:
            trashed = await run.io_bound(_work)
        except Exception as exc:  # noqa: BLE001
            ui.notify(t("duplicates.resolve_error", error=exc), type="negative")
            return
        # Drop the resolved groups from the in-memory report so the card disappears.
        resolved_ids = {gid for gid, _ in groups_with_ids}
        report = state["report"]
        if report is not None:
            report.exact = [g for i, g in enumerate(report.exact)
                            if f"exact:{i}" not in resolved_ids]
            report.probable = [g for i, g in enumerate(report.probable)
                               if f"probable:{i}" not in resolved_ids]
        ui.notify(t("duplicates.resolved", count=trashed), type="positive")
        render_body.refresh()

    def _confirm_resolve(gid: str, group) -> None:
        keeper = _keeper_for(gid, group)
        remove = [p.rel_path for p in group.paths if p.rel_path != keeper]
        dialog = ui.dialog()
        with dialog, ui.card().classes("glass w-full max-w-md rounded-2xl p-4 gap-2"):
            ui.label(t("duplicates.confirm_title")).classes("font-semibold")
            ui.label(t("duplicates.confirm_keep", path=keeper)).classes(
                "text-sm text-emerald-300 break-all")
            ui.label(t("duplicates.will_trash", count=len(remove))).classes(
                "text-sm text-white/70 pt-1")
            for rel in remove:
                ui.label(rel).classes("text-xs text-white/50 break-all pl-2")
            ui.label(t("duplicates.repoint_note")).classes("text-xs text-white/40 pt-1")

            async def confirm() -> None:
                dialog.close()
                await _do_resolve([(gid, group)])

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button(t("settings.cancel"), on_click=dialog.close).props("flat")
                ui.button(t("duplicates.resolve"), icon="cleaning_services",
                          on_click=confirm).classes("accent-grad text-white")
        dialog.open()

    def _confirm_bulk(exact_with_ids: list[tuple[str, object]]) -> None:
        total = sum(len(g.paths) - 1 for _, g in exact_with_ids)
        dialog = ui.dialog()
        with dialog, ui.card().classes("glass w-full max-w-sm rounded-2xl p-4 gap-2"):
            ui.label(t("duplicates.bulk_title")).classes("font-semibold")
            ui.label(t("duplicates.bulk_body", groups=len(exact_with_ids), count=total)).classes(
                "text-sm text-white/70")
            ui.label(t("duplicates.repoint_note")).classes("text-xs text-white/40 pt-1")

            async def confirm() -> None:
                dialog.close()
                await _do_resolve(exact_with_ids)

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button(t("settings.cancel"), on_click=dialog.close).props("flat")
                ui.button(t("duplicates.accept_all"), icon="done_all",
                          on_click=confirm).classes("accent-grad text-white")
        dialog.open()

    # --- group card ----------------------------------------------------------------------
    def _group_card(gid: str, group) -> None:
        with ui.card().classes("glass w-full rounded-2xl p-4 gap-2"):
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                head = f"{group.artist} – {group.title}" if group.artist else group.title
                ui.label(head).classes("font-semibold truncate flex-1 min-w-0")
                if group.tier == "exact":
                    ui.button(t("duplicates.resolve"), icon="cleaning_services",
                              on_click=lambda g=group, i=gid: _confirm_resolve(i, g)) \
                        .props("outline dense").classes("text-white/90 shrink-0")
            options = {p.rel_path: p.rel_path for p in group.paths}
            radio = ui.radio(options, value=_keeper_for(gid, group),
                             on_change=lambda e, i=gid: state["keepers"].__setitem__(i, e.value)) \
                .props("dense").classes("w-full")
            # Annotate each option with its folder tag (rendered under the radio for context).
            with ui.column().classes("w-full gap-0 pl-1 -mt-1"):
                for p in group.paths:
                    tag = _folder_label(p)
                    suffix = t("duplicates.suggested") if p.rel_path == group.suggested_keeper else ""
                    ui.label(f"{tag}{suffix}").classes("text-xs text-white/40 break-all")
            radio.classes("text-sm")

    # --- render --------------------------------------------------------------------------
    @ui.refreshable
    def render_body() -> None:
        st = duplicates.get_analysis_state(uid)
        if st is not None and not st.finished:
            with ui.card().classes("glass w-full rounded-2xl p-8 gap-3 items-center text-center"):
                ui.spinner(size="lg").classes("text-white/60")
                ui.label(t(_PHASE_KEYS.get(st.phase, "duplicates.phase_queued"))) \
                    .classes("text-white/70")
            return

        report = state["report"]
        if report is None:
            with ui.card().classes("glass w-full rounded-2xl p-10 gap-3 items-center text-center"):
                ui.icon("content_copy", size="3rem").classes("text-white/25")
                ui.label(t("duplicates.never")).classes("text-white/60")
                if not has_webdav:
                    ui.label(t("duplicates.no_webdav")).classes("text-amber-300 text-sm")
            return

        exact_ids = [(f"exact:{i}", g) for i, g in enumerate(report.exact)]
        probable_ids = [(f"probable:{i}", g) for i, g in enumerate(report.probable)]

        if not exact_ids and not probable_ids:
            with ui.card().classes("glass w-full rounded-2xl p-10 gap-3 items-center text-center"):
                ui.icon("verified", size="3rem").classes("text-emerald-300/70")
                ui.label(t("duplicates.none_found")).classes("text-white/70")
            return

        # Exact tier.
        with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
            ui.label(t("duplicates.exact_heading", count=len(exact_ids))).classes(
                "text-sm uppercase tracking-widest text-white/50")
            if exact_ids:
                ui.button(t("duplicates.accept_all"), icon="done_all",
                          on_click=lambda: _confirm_bulk(exact_ids)) \
                    .props("outline dense").classes("text-white/90")
        if not exact_ids:
            ui.label(t("duplicates.none_exact")).classes("text-white/40 text-sm")
        for gid, group in exact_ids:
            _group_card(gid, group)

        # Probable tier (collapsed; never bulk-resolved).
        if probable_ids:
            with ui.expansion(t("duplicates.probable_heading", count=len(probable_ids))) \
                    .classes("w-full glass rounded-2xl").props("dense"):
                ui.label(t("duplicates.probable_hint")).classes("text-xs text-white/40 pb-2")
                for gid, group in probable_ids:
                    _group_card(gid, group)

    # Header lives OUTSIDE the refreshable so the Analyze button keeps its identity across
    # refreshes (mirrors the library page).
    with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
        with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
            ui.label(t("duplicates.heading")).classes("text-xl font-semibold accent-text")
            ui.button(t("duplicates.analyze"), icon="search", on_click=analyze) \
                .props("outline").classes("text-white/90")
        ui.label(t("duplicates.intro")).classes("text-xs text-white/50")

    render_body()
    ui.timer(1.5, _poll)
