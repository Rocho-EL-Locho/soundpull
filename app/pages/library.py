"""Library browser page (roadmap 03): browse the WebDAV `ServerTrack` index.

Renders the per-user index as **Artists → Albums → Tracks** (playlist folders in their own
section), with search, header stats, a shared **Rescan** button, per-track / per-album trash
actions, an album-scoped lyrics backfill and — when `navidrome_base_url` is set — a deep link
into Navidrome. Everything is derived from `library_index.library_tree`; this page never
touches the download pipeline or the tag chain (display + file-ops only).
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import datetime, timezone

from nicegui import run, ui
from sqlmodel import select

from app import library_index, library_ops
from app.auth import get_current_user
from app.db import session_scope
from app.i18n import t
from app.models import UserSettings
from app.pages._shared import run_library_task
from app.theme import ghost_button, icon_button, primary_button, secondary_button

log = logging.getLogger("library")


def _scanned_text(when: datetime | None) -> str:
    """Human 'scanned Nh ago' from the stored last-scan time (or 'never')."""
    if when is None:
        return t("library.scanned_never")
    if when.tzinfo is None:  # SQLite hands back naive datetimes — treat as UTC
        when = when.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - when
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return t("library.scanned_recent")
    if hours < 24:
        return t("library.scanned_hours", hours=hours)
    return t("library.scanned_days", days=hours // 24)


def _navidrome_album_url(base: str, album_name: str) -> str:
    """A plain Navidrome search deep link for an album (no API coupling).

    Returns ``""`` unless `base` is an http(s) URL — the value is rendered as an ``<a href>``,
    so a ``javascript:``/``data:`` base would be a (self-)XSS vector; the caller omits the
    link when this is empty. The album name is URL-encoded, so it can't break out either.
    """
    if not re.match(r"https?://", base.strip(), re.IGNORECASE):
        return ""
    filt = urllib.parse.quote(json.dumps({"name": album_name}))
    return f"{base.strip().rstrip('/')}/app/#/album?filter={filt}"


def library_content() -> None:
    """Sub-page builder (mounted by the app-shell ``ui.sub_pages`` router)."""
    with session_scope() as session:
        user = get_current_user(session)
        if user is None:
            ui.navigate.to("/login")
            return
        uid = user.id
        us = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
        has_webdav = bool(us and us.webdav_url)
        navidrome = (us.navidrome_base_url or "").strip() if us else ""
        last_scan = us.last_library_scan_at if us else None

    # Selection + data state. `tree` is loaded once (fast local DB query) and re-loaded only
    # on a rescan / mutation; search filters the loaded tree client-side so typing stays snappy.
    state: dict = {"search": "", "kind": None, "artist": None, "album": None,
                   "tree": None, "last_scan": last_scan}

    def _load_tree():
        with session_scope() as session:
            state["tree"] = library_index.library_tree(session, uid)

    def _reload_meta() -> None:
        with session_scope() as session:
            row = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
            state["last_scan"] = row.last_library_scan_at if row else None

    _load_tree()

    # --- notification result mappers (shared shape with the settings page) ---------------
    def _scan_done(result) -> tuple[str, str]:
        if result is None:  # a scan for this user is already running (shared guard)
            return "warning", t("settings.scan_busy")
        added, pruned, errors = result
        if errors:
            return "warning", t("settings.scan_incomplete", count=added, failed=len(errors))
        return "positive", t("settings.scan_done", count=added, removed=pruned)

    def _backfill_done(result) -> tuple[str, str]:
        written, skipped, missing, errors = result
        if errors:
            return "warning", t("settings.lyrics_backfill_incomplete", written=written,
                                failed=len(errors))
        return "positive", t("settings.lyrics_backfill_done", written=written,
                             skipped=skipped, missing=missing)

    # --- actions -------------------------------------------------------------------------
    async def rescan() -> None:
        from app import jobs

        def _scan():
            # Guarded run: skips if a scheduled/other scan is already walking this library.
            result = jobs.run_scan_sync(uid)
            if result is None:
                return None
            try:  # opportunistic trash purge (roadmap 01) — best-effort, never fails the scan
                library_ops.purge_trash(uid)
            except Exception as exc:  # noqa: BLE001
                log.warning("opportunistic trash purge failed: %s", exc)
            return result

        result = await run_library_task(_scan, running_key="settings.scan_running",
                                        error_key="settings.scan_error", done=_scan_done)
        if result is not None:
            _load_tree()
            _reload_meta()
            render_stats.refresh()
            render_browser.refresh()

    async def _backfill_album(album) -> None:
        await run_library_task(
            lambda: library_index.backfill_lyrics(uid, prefix=album.folder_rel),
            running_key="settings.lyrics_backfill_running",
            error_key="settings.lyrics_backfill_error", done=_backfill_done)

    def _confirm_trash_track(track) -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("glass w-full max-w-sm rounded-2xl p-4 gap-2"):
            ui.label(t("library.confirm_delete_track")).classes("font-semibold")
            ui.label(track.title).classes("text-sm text-white/70 break-all")

            async def confirm() -> None:
                dialog.close()
                try:
                    await run.io_bound(library_ops.trash_track, uid, track.rel_path)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(t("library.delete_error", error=exc), type="negative")
                    return
                ui.notify(t("library.deleted"), type="positive")
                _load_tree()
                render_stats.refresh()
                render_browser.refresh()

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ghost_button(t("settings.cancel"), on_click=dialog.close)
                primary_button(t("library.delete_yes"), icon="delete", on_click=confirm)
        dialog.open()

    def _confirm_trash_album(album) -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("glass w-full max-w-sm rounded-2xl p-4 gap-2"):
            ui.label(t("library.confirm_delete_album")).classes("font-semibold")
            ui.label(album.name).classes("text-sm text-white/70 break-all")

            async def confirm() -> None:
                dialog.close()
                try:
                    await run.io_bound(library_ops.trash_folder, uid, album.folder_rel)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(t("library.delete_error", error=exc), type="negative")
                    return
                ui.notify(t("library.deleted"), type="positive")
                state["album"] = None  # the open folder is gone
                _load_tree()
                render_stats.refresh()
                render_browser.refresh()

            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ghost_button(t("settings.cancel"), on_click=dialog.close)
                primary_button(t("library.delete_yes"), icon="delete", on_click=confirm)
        dialog.open()

    # --- selection -----------------------------------------------------------------------
    def _select_artist(name: str) -> None:
        state.update(kind="artist", artist=name, album=None)
        render_browser.refresh()

    def _select_playlist(folder: str) -> None:
        state.update(kind="playlist", artist=None, album=folder)
        render_browser.refresh()

    def _select_album(folder: str) -> None:
        state["album"] = folder
        render_browser.refresh()

    # --- panes ---------------------------------------------------------------------------
    def _row_button(label: str, count: int, active: bool, on_click) -> None:
        # A clickable flex row (not a ui.button): Quasar's button content wraps, which pushed
        # the count onto a second line for long names. `flex-nowrap` + a shrinking `min-w-0`
        # label keeps every row a single truncated line.
        base = ("w-full flex flex-nowrap items-center justify-between gap-2 px-3 py-2 "
                "rounded-lg cursor-pointer transition")
        active_cls = "accent-grad text-white" if active else "text-white/85 hover:bg-white/10"
        with ui.row().classes(f"{base} {active_cls}").on("click", on_click):
            ui.label(label).classes("truncate flex-1 min-w-0 text-sm")
            ui.label(str(count)).classes("text-xs opacity-70 shrink-0")

    def _artist_pane(view) -> None:
        with ui.card().classes("glass rounded-xl p-3 gap-1 w-full md:flex-1 md:min-w-0 "
                               "max-h-[70vh] overflow-auto"):
            ui.label(t("library.artists")).classes(
                "text-xs uppercase tracking-widest text-white/50 px-1")
            if not view.artists:
                ui.label(t("library.no_artists")).classes("text-white/40 text-sm px-1")
            for artist in view.artists:
                _row_button(artist.name, artist.track_count,
                            state["kind"] == "artist" and state["artist"] == artist.name,
                            lambda a=artist: _select_artist(a.name))
            if view.playlists:
                ui.label(t("library.playlists")).classes(
                    "text-xs uppercase tracking-widest text-white/50 px-1 pt-2")
                for pl in view.playlists:
                    _row_button(pl.name, len(pl.tracks),
                                state["kind"] == "playlist" and state["album"] == pl.folder_rel,
                                lambda p=pl: _select_playlist(p.folder_rel))

    def _album_pane(view) -> None:
        with ui.card().classes("glass rounded-xl p-3 gap-1 w-full md:flex-1 md:min-w-0 "
                               "max-h-[70vh] overflow-auto"):
            ui.label(t("library.albums")).classes(
                "text-xs uppercase tracking-widest text-white/50 px-1")
            if state["kind"] != "artist" or state["artist"] is None:
                ui.label(t("library.pick_artist")).classes("text-white/40 text-sm px-1")
                return
            artist = next((a for a in view.artists if a.name == state["artist"]), None)
            if artist is None:
                ui.label(t("library.pick_artist")).classes("text-white/40 text-sm px-1")
                return
            for album in artist.albums:
                _row_button(album.name, len(album.tracks),
                            state["album"] == album.folder_rel,
                            lambda al=album: _select_album(al.folder_rel))

    def _current_album(view):
        """The album/playlist whose tracks the track-pane should show (or None)."""
        if state["kind"] == "playlist":
            return next((p for p in view.playlists if p.folder_rel == state["album"]), None)
        if state["kind"] == "artist" and state["album"]:
            artist = next((a for a in view.artists if a.name == state["artist"]), None)
            if artist:
                return next((al for al in artist.albums
                             if al.folder_rel == state["album"]), None)
        return None

    def _track_pane(view) -> None:
        album = _current_album(view)
        with ui.card().classes("glass rounded-xl p-3 gap-1 w-full md:flex-1 md:min-w-0 "
                               "max-h-[70vh] overflow-auto"):
            if album is None:
                ui.label(t("library.tracks")).classes(
                    "text-xs uppercase tracking-widest text-white/50 px-1")
                ui.label(t("library.pick_album")).classes("text-white/40 text-sm px-1")
                return
            with ui.row().classes("w-full flex-nowrap items-center gap-1 px-1"):
                ui.label(album.name).classes("font-semibold truncate min-w-0")
            # Album-level actions: delete folder, backfill lyrics, open in Navidrome (albums
            # only — a playlist folder has no Navidrome album to point at).
            with ui.row().classes("items-center gap-1 flex-wrap px-1 pb-1"):
                ghost_button(t("library.delete_album"), icon="delete",
                             on_click=lambda al=album: _confirm_trash_album(al)).props("dense")
                ghost_button(t("library.backfill_album"), icon="lyrics",
                             on_click=lambda al=album: _backfill_album(al)).props("dense")
                nav_url = _navidrome_album_url(navidrome, album.name) \
                    if state["kind"] == "artist" else ""
                if nav_url:  # only for a valid http(s) base (guards a javascript: self-XSS)
                    ui.link(t("library.open_navidrome"), nav_url, new_tab=True) \
                        .classes("text-cyan-300 text-sm self-center no-underline pl-1")
            for track in album.tracks:
                with ui.row().classes("w-full flex-nowrap items-center justify-between "
                                      "gap-2 px-1"):
                    ui.label(track.title).classes("text-sm text-white/85 truncate flex-1 min-w-0")
                    icon_button(icon="delete",
                                on_click=lambda tr=track: _confirm_trash_track(tr)) \
                        .props("size=sm").classes("shrink-0").tooltip(t("library.delete_track"))

    # --- render --------------------------------------------------------------------------
    @ui.refreshable
    def render_stats() -> None:
        tree = state["tree"]
        ui.label(t("library.stats", artists=tree.total_artists, albums=tree.total_albums,
                   tracks=tree.total_tracks) + " · " + _scanned_text(state["last_scan"])) \
            .classes("text-xs text-white/50")

    @ui.refreshable
    def render_browser() -> None:
        tree = state["tree"]
        if tree.total_tracks == 0:
            with ui.card().classes("glass w-full rounded-2xl p-10 gap-3 items-center text-center"):
                ui.icon("library_music", size="3rem").classes("text-white/25")
                ui.label(t("library.empty")).classes("text-white/60")
                if not has_webdav:
                    ui.label(t("library.empty_no_webdav")).classes("text-amber-300 text-sm")
                secondary_button(t("library.empty_scan"), icon="cloud_sync", on_click=rescan)
            return
        view = library_index.filter_tree(tree, state["search"])
        with ui.element("div").classes("w-full flex flex-col md:flex-row gap-4 items-start"):
            _artist_pane(view)
            _album_pane(view)
            _track_pane(view)

    def _on_search(value: str) -> None:
        state["search"] = value or ""
        render_browser.refresh()

    # Header (search + rescan live OUTSIDE the refreshable so typing keeps focus, mirroring
    # the history page); only the stats label and the browser panes refresh on change.
    with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
        with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
            ui.label(t("library.heading")).classes("text-xl font-semibold accent-text")
            secondary_button(t("library.rescan"), icon="cloud_sync", on_click=rescan)
        render_stats()
        ui.input(t("library.search"),
                 on_change=lambda e: _on_search(e.value)) \
            .props("outlined dense dark clearable debounce=300").classes("w-full")

    render_browser()
