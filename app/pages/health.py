"""Library health check page (roadmap 05): audit the library and fix fixable problems.

Mirrors the duplicate-finder page (04): a background run (cheap walk or bounded deep-check batch)
whose progress is polled via `ui.timer`, then one card per check with its findings and a fix
button where a fix exists. Cheap fixes (trash a stray, backfill lyrics, delete an empty folder)
run per finding; deep fixes (unify year, embed cover, write genre) run per album. This page never
touches the download pipeline or the tag chain.
"""
from __future__ import annotations

import logging
import posixpath

from nicegui import run, ui
from sqlmodel import select

from app import health
from app.auth import get_current_user
from app.db import session_scope
from app.i18n import t
from app.models import UserSettings

log = logging.getLogger("health_page")

_PHASE_KEYS = {
    "queued": "health.phase_queued",
    "scanning": "health.phase_scanning",
    "checking": "health.phase_checking",
}

# Render order + which tier each check belongs to. Report-only checks have no fix button.
_CHECK_ORDER = ("lyrics_missing", "stray_file", "empty_folder", "junk_file",
                "year_split", "cover_missing", "genre_missing", "album_tag_missing",
                "corrupt_audio")
_TRASH_CHECKS = {"stray_file", "empty_folder"}   # destructive → confirm dialog


def health_content() -> None:
    """Sub-page builder (mounted by the app-shell ``ui.sub_pages`` router)."""
    with session_scope() as session:
        user = get_current_user(session)
        if user is None:
            ui.navigate.to("/login")
            return
        uid = user.id
        us = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
        has_webdav = bool(us and us.webdav_url)

    state: dict = {"report": health.load_report(uid), "last_finished": True}

    def _reload_report() -> None:
        state["report"] = health.load_report(uid)

    # --- run lifecycle -------------------------------------------------------------------
    def _start(mode: str, **kw) -> None:
        if not health.start_health(uid, mode, **kw):
            ui.notify(t("health.busy"), type="warning")
            return
        state["last_finished"] = False
        render_body.refresh()

    def _poll() -> None:
        st = health.get_health_state(uid)
        if st is None:
            return
        if not st.finished:
            render_body.refresh()
            return
        if not state["last_finished"]:
            state["last_finished"] = True
            if st.error:
                ui.notify(t("health.error", error=st.error), type="negative")
            else:
                ui.notify(t("health.done", count=st.finding_count), type="positive")
                _reload_report()
            render_body.refresh()

    # --- fixes ---------------------------------------------------------------------------
    def _prune_cheap(check_id: str, rel_path: str) -> None:
        report = state["report"]
        if report is None:
            return
        if check_id == "lyrics_missing":
            folder = posixpath.dirname(rel_path)  # a folder backfill fixes every sibling too
            report.cheap = [f for f in report.cheap
                            if not (f.check_id == "lyrics_missing"
                                    and posixpath.dirname(f.rel_path) == folder)]
        else:
            report.cheap = [f for f in report.cheap
                            if not (f.check_id == check_id and f.rel_path == rel_path)]

    async def _do_cheap_fix(finding) -> None:
        try:
            res = await run.io_bound(health.fix_finding, uid, finding.check_id, finding.rel_path)
        except Exception as exc:  # noqa: BLE001
            ui.notify(t("health.fix_error", error=exc), type="negative")
            return
        if not res.ok:
            ui.notify(t("health.fix_error", error=res.error or "?"), type="negative")
            return
        _prune_cheap(finding.check_id, finding.rel_path)
        await run.io_bound(health.save_report, uid, state["report"])
        ui.notify(t("health.fixed"), type="positive")
        render_body.refresh()

    async def _do_deep_fix(finding) -> None:
        album = finding.rel_path if finding.check_id == "year_split" \
            else posixpath.dirname(finding.rel_path)
        try:
            res = await run.io_bound(health.fix_album, uid, album, {finding.check_id})
        except Exception as exc:  # noqa: BLE001
            ui.notify(t("health.fix_error", error=exc), type="negative")
            return
        if not res.ok:
            ui.notify(t("health.fix_error", error=res.error or "?"), type="negative")
            return
        fixed = set(res.fixed_paths)
        report = state["report"]
        if report is not None:
            report.deep = [f for f in report.deep
                           if not (f.check_id == finding.check_id and f.rel_path in fixed)]
            await run.io_bound(health.save_report, uid, report)
        ui.notify(t("health.fixed"), type="positive")
        render_body.refresh()

    def _confirm_then(finding, action) -> None:
        """Trash fixes get a confirm dialog; additive fixes run straight away."""
        if finding.check_id not in _TRASH_CHECKS:
            action(finding)
            return
        dialog = ui.dialog()
        with dialog, ui.card().classes("glass w-full max-w-sm rounded-2xl p-4 gap-2"):
            ui.label(t("health.confirm_trash")).classes("font-semibold")
            ui.label(finding.rel_path).classes("text-xs text-white/50 break-all")

            async def confirm() -> None:
                dialog.close()
                await action(finding)

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button(t("settings.cancel"), on_click=dialog.close).props("flat")
                ui.button(t("health.fix"), icon="cleaning_services",
                          on_click=confirm).classes("accent-grad text-white")
        dialog.open()

    # --- rendering -----------------------------------------------------------------------
    def _check_card(check_id: str, findings: list, is_deep: bool) -> None:
        action = _do_deep_fix if is_deep else _do_cheap_fix
        with ui.card().classes("glass w-full rounded-2xl p-4 gap-1"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(t(f"health.check.{check_id}")).classes("font-semibold flex-1 min-w-0")
                ui.badge(str(len(findings))).props("color=primary")
            ui.label(t(f"health.check_desc.{check_id}")).classes("text-xs text-white/40")
            with ui.expansion(t("health.show_findings", count=len(findings))).classes("w-full") \
                    .props("dense"):
                for f in findings:
                    with ui.row().classes("w-full flex-nowrap items-center gap-2 py-0.5"):
                        ui.label(f.detail or f.rel_path).classes(
                            "text-xs text-white/60 truncate flex-1 min-w-0").tooltip(f.rel_path)
                        if f.fixable:
                            ui.button(icon="build",
                                      on_click=lambda fi=f: _confirm_then(fi, action)) \
                                .props("flat dense round size=sm").classes("text-white/70 shrink-0") \
                                .tooltip(t("health.fix"))

    @ui.refreshable
    def render_body() -> None:
        st = health.get_health_state(uid)
        if st is not None and not st.finished:
            with ui.card().classes("glass w-full rounded-2xl p-8 gap-3 items-center text-center"):
                ui.spinner(size="lg").classes("text-white/60")
                label = t(_PHASE_KEYS.get(st.phase, "health.phase_queued"))
                if st.phase == "checking" and st.total_count:
                    label += f" ({st.checked_count}/{st.total_count})"
                ui.label(label).classes("text-white/70")
            return

        report = state["report"]
        if report is None:
            with ui.card().classes("glass w-full rounded-2xl p-10 gap-3 items-center text-center"):
                ui.icon("health_and_safety", size="3rem").classes("text-white/25")
                ui.label(t("health.never")).classes("text-white/60")
                if not has_webdav:
                    ui.label(t("health.no_webdav")).classes("text-amber-300 text-sm")
            return

        by_check: dict[str, list] = {}
        for f in report.cheap + report.deep:
            by_check.setdefault(f.check_id, []).append(f)

        if not by_check:
            with ui.card().classes("glass w-full rounded-2xl p-10 gap-3 items-center text-center"):
                ui.icon("verified", size="3rem").classes("text-emerald-300/70")
                ui.label(t("health.all_clear")).classes("text-white/70")
        for check_id in _CHECK_ORDER:
            findings = by_check.get(check_id)
            if findings:
                _check_card(check_id, findings, is_deep=check_id in health.DEEP_CHECKS)

        checked = len(report.checked_albums)
        ui.label(t("health.deep_progress", count=checked)).classes("text-xs text-white/40 pt-1")

    # Header + action buttons OUTSIDE the refreshable (keep identity across refreshes).
    with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
        with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
            ui.label(t("health.heading")).classes("text-xl font-semibold accent-text")
            with ui.row().classes("items-center gap-2"):
                ui.button(t("health.run_cheap"), icon="search",
                          on_click=lambda: _start("cheap")).props("outline").classes("text-white/90")
                ui.button(t("health.run_deep"), icon="travel_explore",
                          on_click=lambda: _start("deep_batch")).props("outline") \
                    .classes("text-white/90")
        ui.label(t("health.intro")).classes("text-xs text-white/50")

    render_body()
    ui.timer(1.5, _poll)
