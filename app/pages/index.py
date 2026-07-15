"""Download page: form (URL prefillable via ?url=) + live progress cards."""
from __future__ import annotations

from datetime import datetime, timezone

from nicegui import run, ui

from app import search
from app.auth import get_current_user
from app.db import session_scope
from app.fix_music_tags import TagOptions
from app.genres import DEFAULT_GENRE
from app.i18n import audio_format_labels, genre_options, t
from app.jobs import JobState, get_user_jobs, start_job, tag_options_from_settings
from app.pipeline import is_supported_url, normalize_audio_format
from app.sources import detect_source, suggest_mode
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
            if js.warning:  # index update failed (#38), or a partial delivery (throttle/403)
                # warning is an i18n key; the partial note carries counts ("N von M") that
                # `t()` fills in (and ignores for the count-less index warning).
                ui.label(t(js.warning, failed=js.failed_tracks, total=js.total_tracks)).classes(
                    "text-amber-400 text-xs")
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
        # In-app YouTube Music search (roadmap 07): sits ABOVE the URL field. Built into this
        # placeholder at the END of the card so its click handlers can reference url_in / mode_tgl /
        # start_btn (defined further down). Fails soft — a search error never touches the form.
        search_slot = ui.column().classes("w-full gap-2")

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

        # Bandcamp quality hint (roadmap 11): shown only when a Bandcamp URL is detected —
        # its free streams are ~128 kbps MP3, so "Original" (remux) is the honest choice.
        with ui.row().classes("w-full items-start gap-2") as bandcamp_hint:
            ui.icon("info", size="18px").classes("text-amber-300/80 mt-0.5")
            ui.label(t("index.bandcamp_hint")).classes("text-xs text-amber-200/80 flex-1")
        bandcamp_hint.set_visibility(False)

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

        def _apply_artist_dedup_default() -> None:
            # Artist runs default to skipping existing tracks — a whole-discography re-download
            # shouldn't re-pull everything — but the toggle stays editable, so you CAN turn
            # skipping off to force a full re-download.
            if mode_tgl.value == "artist":
                dedup_sw.set_value(True)

        # URL intelligence (roadmap feature 02): pasting a URL pre-selects the most likely
        # mode. We must not fight a manual choice — once the user picks a mode themselves,
        # `mode_state["manual"]` latches and auto-suggestion stops for the rest of the
        # session, so a later URL edit never clobbers their pick. `suppress` distinguishes
        # our own programmatic `set_value` (which also fires on_value_change) from a real click.
        mode_state = {"manual": False}
        suppress = {"on": False}

        def _on_mode_change() -> None:
            if not suppress["on"]:
                mode_state["manual"] = True  # a genuine user pick — stop auto-suggesting
            _apply_artist_dedup_default()

        def _suggest_mode_from_url() -> None:
            if mode_state["manual"]:
                return  # respect the user's explicit choice — never override it
            guess = suggest_mode(url_in.value or "")
            if not guess:
                return
            suppress["on"] = True
            try:
                mode_tgl.set_value(guess)
            finally:
                suppress["on"] = False

        def _update_source_hint() -> None:
            spec = detect_source(url_in.value or "")
            bandcamp_hint.set_visibility(bool(spec and spec.key == "bandcamp"))

        def _on_url_change() -> None:
            _suggest_mode_from_url()
            _update_source_hint()

        mode_tgl.on_value_change(lambda: _on_mode_change())
        url_in.on_value_change(lambda: _on_url_change())
        _select_dest(d_dest)  # sets initial card highlight + dedup visibility
        if (url or "").strip():
            _on_url_change()  # a prefilled ?url= gets its mode suggested + hint once
        _apply_artist_dedup_default()  # default artist mode to skip-existing on

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

        start_btn = ui.button(t("index.start_button"), icon="download", on_click=start) \
            .props("unelevated").classes("accent-grad text-white hover-glow self-end px-6")

        # --- Search section (built into the top placeholder now that the form widgets exist) ---
        _MODE_FOR_KIND = {"song": "single", "album": "album",
                          "playlist": "playlist", "artist": "artist"}
        search_state: dict = {"results": []}

        def _fill_from_result(r: search.SearchResult, url: str) -> None:
            # Set the mode as an EXPLICIT choice (latch manual) so the URL's auto-suggestion
            # (feature 02) can't override it; then set the URL (its handler early-returns on manual).
            mode_state["manual"] = True
            mode_tgl.set_value(_MODE_FOR_KIND.get(r.kind, "album"))
            url_in.value = url
            search_state["results"] = []
            render_results.refresh()
            start_btn.run_method("focus")

        async def _pick(r: search.SearchResult) -> None:
            url = r.url
            if url is None:  # an album whose OLAK5uy_ id wasn't in the search payload
                try:
                    url = await run.io_bound(search.resolve_album_url, r.browse_id)
                except Exception:  # noqa: BLE001 - fail soft
                    ui.notify(t("search.failed"), type="warning")
                    return
            _fill_from_result(r, url)

        _KIND_GROUPS = (("song", "search.songs"), ("album", "search.albums"),
                        ("artist", "search.artists"), ("playlist", "search.playlists"))

        @ui.refreshable
        def render_results() -> None:
            results = search_state["results"]
            if not results:
                return
            with ui.column().classes("w-full gap-3"):
                for kind, label_key in _KIND_GROUPS:
                    group = [r for r in results if r.kind == kind]
                    if not group:
                        continue
                    ui.label(t(label_key)).classes(
                        "text-xs uppercase tracking-widest text-white/50")
                    with ui.row().classes("w-full flex-wrap gap-2"):
                        for r in group:
                            _result_card(r)

        def _result_card(r: search.SearchResult) -> None:
            card = ui.element("div").classes(
                "sp-dest-card cursor-pointer flex items-center gap-3 !w-auto max-w-full") \
                .on("click", lambda rr=r: _pick(rr))
            with card:
                if r.thumbnail:
                    ui.image(r.thumbnail).classes("w-10 h-10 rounded object-cover shrink-0")
                else:
                    ui.icon("music_note", size="24px").classes("text-white/40 shrink-0")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(r.title or "…").classes("sp-dest-title truncate")
                    if r.artist:
                        ui.label(r.artist).classes("text-xs text-white/50 truncate")

        async def _do_search() -> None:
            q = (search_in.value or "").strip()
            if not q:
                return
            try:
                results = await run.io_bound(search.search_music, q)
            except Exception:  # noqa: BLE001 - SearchError or anything else → soft warning
                ui.notify(t("search.failed"), type="warning")
                return
            search_state["results"] = results
            render_results.refresh()
            if not results:
                ui.notify(t("search.no_results"), type="info")

        with search_slot:
            with ui.column().classes("w-full gap-1.5"):
                _field_label(t("search.label"))
                with ui.row().classes("w-full gap-2 items-center flex-nowrap"):
                    search_in = ui.input(placeholder=t("search.placeholder")) \
                        .props("outlined dense dark clearable").classes("flex-1 min-w-0")
                    search_in.on("keydown.enter", lambda: _do_search())
                    ui.button(t("search.button"), icon="search", on_click=_do_search) \
                        .props("unelevated dense").classes("accent-grad text-white shrink-0")
            render_results()

    ui.label(t("index.active_heading")).classes("text-xs uppercase tracking-widest text-white/50 mt-2")
    render_jobs()
    ui.timer(1.0, render_jobs.refresh)
