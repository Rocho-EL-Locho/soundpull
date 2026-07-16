"""Dedicated search & browse page (YouTube Music).

Split out of the download page so results have room and can be navigated: search → click an
artist to see all their albums/singles/songs → click an album to see its tracks. Songs download
on click; albums/artists open a detail view with a download action. Every download starts
immediately with the user's saved defaults (genre / quality / destination / dedup / lyrics) and
appears as a job on the download page — this page only produces URLs, so metadata parity is
unaffected. Search is YouTube Music only (ytmusicapi); other sources are pasted on the download
page.
"""
from __future__ import annotations

from nicegui import run, ui

from app import search
from app.auth import get_current_user
from app.db import session_scope
from app.genres import DEFAULT_GENRE
from app.i18n import t
from app.jobs import start_job
from app.pipeline import is_supported_url, normalize_audio_format
from app.theme import ghost_button, icon_button, primary_button, secondary_button

_SEARCH_STEP = 5
_SEARCH_MAX = 30
_MODE_FOR_KIND = {"song": "single", "album": "album", "playlist": "playlist", "artist": "artist"}
# Result groups on the search view, in display order.
_KIND_GROUPS = (("song", "search.songs"), ("album", "search.albums"),
                ("artist", "search.artists"), ("playlist", "search.playlists"))


def discover_content() -> None:
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

    # view ∈ {search, artist, album}; `prev` is the view to return to from an album.
    state: dict = {"view": "search", "results": [], "query": "", "limit": _SEARCH_STEP,
                   "artist": None, "artist_url": None, "album": None, "prev": "search"}

    # --- download (immediate, with saved defaults) ---------------------------------------
    def _start(url: str, mode: str, title: str) -> None:
        # Defense-in-depth: these URLs are built by search.py from YouTube ids (always
        # music.youtube.com), but validate before queuing anyway — same gate the download page
        # applies, so an unexpected/empty URL never reaches yt-dlp.
        if not is_supported_url(url):
            ui.notify(t("index.notify_bad_url"), type="warning")
            return
        # Artist runs default to skip-existing on WebDAV (a discography re-pull shouldn't refetch
        # everything); dedup only applies to WebDAV at all.
        dedup = (mode == "artist") or (d_dedup and d_dest == "webdav")
        try:
            start_job(user_id=uid, url=url, genre=d_genre, mode=mode, destination_type=d_dest,
                      audio_format=d_audio, tag_options=None, dedup=dedup, fetch_lyrics=d_lyrics)
        except Exception as exc:  # noqa: BLE001 - surface config/validation errors
            ui.notify(str(exc), type="negative")
            return
        ui.notify(t("discover.started", title=title or "…"), type="positive")

    async def _download_result(r: search.SearchResult) -> None:
        url = r.url
        if url is None and r.browse_id:  # an album whose playlist id needs resolving
            try:
                url = await run.io_bound(search.resolve_album_url, r.browse_id)
            except Exception:  # noqa: BLE001 - fail soft
                ui.notify(t("search.failed"), type="warning")
                return
        if not url:
            ui.notify(t("search.failed"), type="warning")
            return
        _start(url, _MODE_FOR_KIND.get(r.kind, "single"), r.title)

    # --- drill-down navigation -----------------------------------------------------------
    async def _open_artist(r: search.SearchResult) -> None:
        if not r.browse_id:
            await _download_result(r)
            return
        note = ui.notification(t("discover.loading"), type="ongoing", spinner=True, timeout=None)
        try:
            detail = await run.io_bound(search.get_artist, r.browse_id)
        except Exception:  # noqa: BLE001 - fail soft
            ui.notify(t("discover.open_error"), type="warning")
            return
        finally:
            note.dismiss()
        state.update(view="artist", artist=detail, artist_url=r.url, prev="search")
        render.refresh()

    async def _open_album(r: search.SearchResult, prev: str) -> None:
        if not r.browse_id:  # no browse id → can't fetch a tracklist; just download it
            await _download_result(r)
            return
        note = ui.notification(t("discover.loading"), type="ongoing", spinner=True, timeout=None)
        try:
            detail = await run.io_bound(search.get_album_detail, r.browse_id)
        except Exception:  # noqa: BLE001 - fail soft
            ui.notify(t("discover.open_error"), type="warning")
            return
        finally:
            note.dismiss()
        state.update(view="album", album=detail, prev=prev)
        render.refresh()

    def _go(view: str) -> None:
        state["view"] = view
        render.refresh()

    # --- cards ---------------------------------------------------------------------------
    def _card(r: search.SearchResult, on_click, subtitle: str | None = None) -> None:
        card = ui.element("div").classes(
            "sp-dest-card cursor-pointer flex items-center gap-3 w-full min-w-0") \
            .on("click", on_click)
        with card:
            if r.thumbnail:
                ui.image(r.thumbnail).classes("w-10 h-10 rounded object-cover shrink-0")
            else:
                ui.icon("music_note", size="24px").classes("text-white/40 shrink-0")
            with ui.column().classes("gap-0 min-w-0 flex-1"):
                ui.label(r.title or "…").classes("sp-dest-title truncate")
                sub = subtitle if subtitle is not None else r.artist
                if sub:
                    ui.label(sub).classes("text-xs text-white/50 truncate")

    def _grid(items, on_click, subtitle_of=lambda r: None) -> None:
        with ui.element("div").classes(
                "w-full grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2"):
            for r in items:
                _card(r, (lambda rr=r: on_click(rr)), subtitle_of(r))

    # --- search flow ---------------------------------------------------------------------
    async def _run_search(q: str) -> None:
        try:
            results = await run.io_bound(search.search_music, q, state["limit"])
        except Exception:  # noqa: BLE001 - SearchError or anything else → soft warning
            ui.notify(t("search.failed"), type="warning")
            return
        state["results"] = results
        render.refresh()
        if not results:
            ui.notify(t("search.no_results"), type="info")

    async def _do_search() -> None:
        q = (search_in.value or "").strip()
        if not q:
            return
        state.update(query=q, limit=_SEARCH_STEP)
        await _run_search(q)

    async def _show_more() -> None:
        if not state["query"]:
            return
        state["limit"] = min(state["limit"] + _SEARCH_STEP, _SEARCH_MAX)
        await _run_search(state["query"])

    def _on_result(r: search.SearchResult) -> None:
        # Songs/playlists download on click; artists/albums open a browse view.
        if r.kind == "artist":
            return _open_artist(r)
        if r.kind == "album":
            return _open_album(r, "search")
        return _download_result(r)

    # --- views ---------------------------------------------------------------------------
    def _render_search() -> None:
        results = state["results"]
        if not results:
            ui.label(t("discover.empty_prompt")).classes("text-white/40 text-sm")
            return
        with ui.column().classes("w-full gap-3"):
            for kind, label_key in _KIND_GROUPS:
                group = [r for r in results if r.kind == kind]
                if not group:
                    continue
                ui.label(t(label_key)).classes(
                    "text-xs uppercase tracking-widest text-white/50")
                _grid(group, _on_result)
            if state["limit"] < _SEARCH_MAX:
                with ui.row().classes("w-full justify-center pt-1"):
                    secondary_button(t("search.more"), icon="expand_more",
                                     on_click=_show_more).props("dense")
            with ui.row().classes("w-full items-start gap-2 pt-1"):
                ui.icon("cookie", size="16px").classes("text-white/35 mt-0.5")
                ui.label(t("search.cookie_hint")).classes("text-xs text-white/40 flex-1")

    def _detail_header(title: str, subtitle: str, thumbnail: str | None) -> None:
        with ui.row().classes("w-full items-center gap-4"):
            if thumbnail:
                ui.image(thumbnail).classes("w-20 h-20 rounded-lg object-cover shrink-0")
            with ui.column().classes("gap-0 min-w-0 flex-1"):
                ui.label(title or "…").classes("text-xl font-semibold accent-text truncate")
                if subtitle:
                    ui.label(subtitle).classes("text-sm text-white/60 truncate")

    def _render_artist() -> None:
        art = state["artist"]
        ghost_button(t("discover.back"), icon="arrow_back",
                     on_click=lambda: _go("search")).props("dense")
        with ui.card().classes("glass w-full rounded-2xl p-5 gap-4"):
            _detail_header(art.name, "", art.thumbnail)
            if state["artist_url"]:
                primary_button(t("discover.download_all"), icon="download",
                               on_click=lambda: _start(state["artist_url"], "artist", art.name)) \
                    .props("dense").classes("self-start")
            for items, label_key in ((art.albums, "search.albums"),
                                     (art.singles, "discover.singles"),
                                     (art.songs, "search.songs")):
                if not items:
                    continue
                ui.label(t(label_key)).classes(
                    "text-xs uppercase tracking-widest text-white/50 pt-1")
                if label_key == "search.songs":
                    _grid(items, _download_result)                 # songs download on click
                else:
                    _grid(items, lambda r: _open_album(r, "artist"),  # albums/singles → browse
                          subtitle_of=lambda r: r.title and "")

    def _render_album() -> None:
        alb = state["album"]
        ghost_button(t("discover.back"), icon="arrow_back",
                     on_click=lambda: _go(state["prev"])).props("dense")
        with ui.card().classes("glass w-full rounded-2xl p-5 gap-3"):
            _detail_header(alb.title, alb.artist, alb.thumbnail)
            if alb.url:
                primary_button(t("discover.download_album"), icon="download",
                               on_click=lambda: _start(alb.url, "album", alb.title)) \
                    .props("dense").classes("self-start")
            for i, tr in enumerate(alb.tracks, start=1):
                with ui.row().classes("w-full flex-nowrap items-center gap-2 py-0.5 "
                                      "border-t border-white/5"):
                    ui.label(str(i)).classes("text-xs text-white/35 w-6 shrink-0 text-right")
                    ui.label(tr.title or "…").classes(
                        "text-sm text-white/85 truncate flex-1 min-w-0")
                    icon_button(icon="download", on_click=lambda r=tr: _download_result(r)) \
                        .props("size=sm").classes("shrink-0").tooltip(t("discover.download"))

    @ui.refreshable
    def render() -> None:
        view = state["view"]
        if view == "artist" and state["artist"] is not None:
            _render_artist()
        elif view == "album" and state["album"] is not None:
            _render_album()
        else:
            _render_search()

    # --- page shell ----------------------------------------------------------------------
    with ui.row().classes("items-center gap-3"):
        ui.icon("search", size="28px").classes("accent-text")
        ui.label(t("discover.title")).classes("text-3xl font-bold text-white")
    ui.label(t("discover.subtitle")).classes("text-white/50 text-sm")

    async def _search_click() -> None:
        state["view"] = "search"   # a fresh query always returns to the results view
        await _do_search()

    with ui.card().classes("glass w-full rounded-2xl p-6 gap-4"):
        with ui.row().classes("w-full gap-2 items-center flex-nowrap"):
            search_in = ui.input(placeholder=t("search.placeholder")) \
                .props("outlined dense dark clearable").classes("flex-1 min-w-0")
            search_in.on("keydown.enter", _search_click)
            primary_button(t("search.button"), icon="search", on_click=_search_click) \
                .props("dense").classes("shrink-0")
        render()
