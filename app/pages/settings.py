"""Settings page: per-user defaults, WebDAV target, and the bookmarklet."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select

from nicegui import run, ui

from app.auth import get_current_user
from app.config import settings as app_settings
from app.db import session_scope
from app.fix_music_tags import TAG_OPTION_FIELDS, TagOptions
from app.genres import ALLOWED_GENRES
from app.i18n import audio_format_labels, t
from app.models import UserSettings
from app.pipeline import normalize_audio_format
from app.security import decrypt_secret, encrypt_secret
from app.theme import frame, tag_option_switches
from app.webdav_util import list_dirs, make_client


@ui.page("/settings")
def settings_page() -> None:
    with frame("settings"):
        with session_scope() as session:
            user = get_current_user(session)
            if user is None:
                ui.navigate.to("/login")
                return
            uid = user.id
            us = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
            if us is None:
                us = UserSettings(user_id=uid)
                session.add(us)
                session.flush()
            dest = us.destination_type if us.destination_type in ("browser", "webdav") else "browser"
            snap = {
                "genre": us.default_genre, "mode": us.default_mode, "dest": dest,
                "audio": normalize_audio_format(us.default_audio_format),
                "tags": {f: bool(getattr(us, f"tag_{f}")) for f in TAG_OPTION_FIELDS},
                "wd_url": us.webdav_url or "", "wd_user": us.webdav_username or "",
                "wd_folder": us.webdav_folder or "", "has_pw": us.has_webdav_password,
                "dedup": bool(us.dedup_skip_existing),
                # Only the "is one stored?" flag — never the plaintext cookie (issue #9).
                "has_cookie": us.has_youtube_cookies,
            }

        with ui.card().classes("glass w-full rounded-2xl p-6 gap-4"):
            ui.label(t("settings.profile_heading")).classes("text-xl font-semibold accent-text")
            with ui.row().classes("w-full gap-3 items-end"):
                genre_sel = ui.select(ALLOWED_GENRES, value=snap["genre"],
                                      label=t("settings.default_genre")) \
                    .props("outlined dense dark").classes("flex-1 min-w-32")
                with ui.column().classes("gap-1"):
                    ui.label(t("settings.default_mode")).classes("text-xs text-white/50")
                    mode_tgl = ui.toggle({"album": t("common.album"), "single": t("common.single"),
                                          "playlist": t("common.playlist")}, value=snap["mode"]) \
                        .props("toggle-color=primary unelevated no-caps").classes("glass rounded-lg")
            audio_sel = ui.select(audio_format_labels(), value=snap["audio"],
                                  label=t("settings.default_audio")) \
                .props("outlined dense dark").classes("w-full")
            dest_sel = ui.select({"browser": t("dest.browser"), "webdav": t("dest.webdav")},
                                 value=snap["dest"], label=t("settings.default_dest")) \
                .props("outlined dense dark").classes("w-full")

        with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
            ui.label(t("meta.heading")).classes("text-lg font-semibold")
            tag_switches = tag_option_switches(TagOptions(**snap["tags"]))

        # YouTube cookie (issue #9). Defined before the WebDAV card so the shared
        # save() closure below can persist it. The stored cookie is never echoed
        # back — only a "set" state via the placeholder.
        with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
            ui.label(t("settings.cookie_heading")).classes("text-lg font-semibold")
            ui.label(t("settings.cookie_desc")).classes("text-xs text-white/50")
            cookie_placeholder = t("settings.cookie_placeholder_set") if snap["has_cookie"] \
                else t("settings.cookie_label")
            cookie_ta = ui.textarea(t("settings.cookie_label"), placeholder=cookie_placeholder) \
                .props("outlined dense dark autogrow").classes("w-full")
            cookie_clear = ui.switch(t("settings.cookie_clear"), value=False) \
                .props("dark").classes("text-sm")
            if not snap["has_cookie"]:
                cookie_clear.set_visibility(False)  # nothing to remove yet

        with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
            ui.label(t("settings.webdav_heading")).classes("text-lg font-semibold")
            ui.label(t("settings.webdav_desc")).classes("text-xs text-white/50")
            wd_url = ui.input(t("settings.webdav_url_label"), value=snap["wd_url"],
                              placeholder="https://cloud.example.org/remote.php/dav/files/<user>/") \
                .props("outlined dense dark").classes("w-full")
            wd_user = ui.input(t("settings.username"), value=snap["wd_user"]) \
                .props("outlined dense dark autocomplete=off").classes("w-full")
            pw_placeholder = t("settings.password_placeholder_set") if snap["has_pw"] \
                else t("settings.password")
            # autocomplete=new-password marks this as a field for *setting* a credential, not
            # logging in — stops the browser from proactively offering saved passwords on load
            # (see issue #6). Pairs with autocomplete=off on the username so the two aren't
            # detected as a login form.
            wd_pass = ui.input(t("settings.password"), password=True, placeholder=pw_placeholder) \
                .props("outlined dense dark autocomplete=new-password").classes("w-full")

            folder_state = {"path": snap["wd_folder"]}

            def _folder_text() -> str:
                p = folder_state["path"]
                return t("settings.folder_label", path=p) if p else t("settings.folder_root")

            async def _resolve_password() -> str:
                if (wd_pass.value or "").strip():
                    return wd_pass.value.strip()
                with session_scope() as session:
                    row = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
                    if row and row.webdav_password_enc:
                        return decrypt_secret(row.webdav_password_enc)
                return ""

            async def _open_picker(client) -> None:
                cur = {"path": folder_state["path"] or ""}
                dialog = ui.dialog()
                with dialog, ui.card().classes("glass w-full max-w-md rounded-2xl p-4 gap-2"):
                    ui.label(t("settings.picker_heading")).classes("font-semibold")
                    crumb = ui.label().classes("text-xs text-white/60")
                    lst = ui.column().classes("w-full gap-1 max-h-72 overflow-auto")

                    async def go_up() -> None:
                        cur["path"] = "/".join(cur["path"].rstrip("/").split("/")[:-1])
                        await load()

                    async def enter(full: str) -> None:
                        cur["path"] = full
                        await load()

                    async def load() -> None:
                        crumb.text = "/" + (cur["path"] or "")
                        lst.clear()
                        try:
                            dirs = await run.io_bound(list_dirs, client, cur["path"])
                        except Exception as exc:  # noqa: BLE001
                            with lst:
                                ui.label(t("settings.picker_error", error=exc)).classes("text-red-400 text-sm")
                            return
                        with lst:
                            if cur["path"]:
                                ui.button(t("settings.picker_back"), on_click=go_up) \
                                    .props("flat dense no-caps").classes("text-white/80")
                            if not dirs:
                                ui.label(t("settings.picker_no_sub")).classes("text-white/40 text-sm")
                            for name, full in dirs:
                                ui.button(f"📁 {name}", on_click=lambda f=full: enter(f)) \
                                    .props("flat dense no-caps align=left").classes("w-full text-white/90")

                    def choose() -> None:
                        folder_state["path"] = cur["path"]
                        folder_lbl.text = _folder_text()
                        dialog.close()
                        ui.notify(t("settings.folder_chosen", path=cur["path"]), type="positive")

                    with ui.row().classes("w-full justify-end gap-2 pt-2"):
                        ui.button(t("settings.cancel"), on_click=dialog.close).props("flat")
                        ui.button(t("settings.picker_choose"), icon="check", on_click=choose) \
                            .classes("accent-grad text-white")
                await load()
                dialog.open()

            async def browse() -> None:
                url = (wd_url.value or "").strip()
                if not url:
                    ui.notify(t("settings.notify_need_url"), type="warning")
                    return
                pw = await _resolve_password()
                if not pw:
                    ui.notify(t("settings.notify_need_pw"), type="warning")
                    return
                try:
                    client = make_client(url, (wd_user.value or "").strip(), pw)
                    await run.io_bound(client.ls, "")
                except Exception as exc:  # noqa: BLE001
                    ui.notify(t("settings.notify_conn_failed", error=exc), type="negative")
                    return
                await _open_picker(client)

            with ui.row().classes("items-center gap-3 flex-wrap"):
                ui.button(t("settings.connect_button"), icon="folder_open", on_click=browse) \
                    .props("unelevated").classes("accent-grad text-white")
                folder_lbl = ui.label(_folder_text()).classes("text-sm text-white/70")

            # Server library scan (issue #21): index tracks already on the server so
            # playlist sync only fetches new ones. Runs off-thread (WebDAV walk is I/O).
            ui.label(t("settings.scan_desc")).classes("text-xs text-white/50")

            async def scan_server() -> None:
                from app import library_index

                ui.notify(t("settings.scan_running"), type="ongoing")
                try:
                    added = await run.io_bound(library_index.scan_webdav, uid)
                except Exception as exc:  # noqa: BLE001 - surface config/connection errors
                    ui.notify(t("settings.scan_error", error=exc), type="negative")
                    return
                ui.notify(t("settings.scan_done", count=added), type="positive")

            ui.button(t("settings.scan_button"), icon="cloud_sync", on_click=scan_server) \
                .props("outline").classes("text-white/90 self-start")

            # Dedup (issue #31): skip already-present tracks; reference them in playlist m3u.
            ui.label(t("settings.dedup_desc")).classes("text-xs text-white/50 mt-2")
            dedup_sw = ui.switch(t("settings.dedup_label"), value=snap["dedup"]) \
                .props("dark").classes("text-sm")

            def save() -> None:
                with session_scope() as session:
                    row = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
                    if row is None:
                        row = UserSettings(user_id=uid)
                        session.add(row)
                    row.default_genre = genre_sel.value
                    row.default_mode = mode_tgl.value
                    row.default_audio_format = audio_sel.value
                    row.destination_type = dest_sel.value
                    for field, switch in tag_switches.items():
                        setattr(row, f"tag_{field}", bool(switch.value))
                    row.webdav_url = (wd_url.value or "").strip() or None
                    row.webdav_folder = folder_state["path"] or None
                    row.webdav_username = (wd_user.value or "").strip() or None
                    row.dedup_skip_existing = bool(dedup_sw.value)
                    if (wd_pass.value or "").strip():
                        row.webdav_password_enc = encrypt_secret(wd_pass.value.strip())
                    # YouTube cookie (issue #9): remove wins; else store new; else keep.
                    if cookie_clear.value:
                        row.youtube_cookies_enc = None
                    elif (cookie_ta.value or "").strip():
                        row.youtube_cookies_enc = encrypt_secret(cookie_ta.value.strip())
                    row.updated_at = datetime.now(timezone.utc)
                    session.add(row)
                ui.notify(t("settings.saved"), type="positive")

            ui.button(t("settings.save_button"), icon="save", on_click=save) \
                .props("unelevated").classes("accent-grad text-white hover-glow self-end px-6")

        # Bookmarklet — dragging a javascript: link is blocked in many browsers,
        # so the reliable path is: copy the code, create a bookmark, paste as URL.
        bm = ("javascript:(()=>{window.open('" + app_settings.app_base_url +
              "/?url='+encodeURIComponent(location.href),'_blank')})()")
        with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
            ui.label(t("settings.bookmarklet_heading")).classes("text-lg font-semibold")
            with ui.column().classes("gap-1 text-xs text-white/60"):
                ui.label(t("settings.bm_setup"))
                ui.label(t("settings.bm_step1"))
                ui.label(t("settings.bm_step2"))
                ui.label(t("settings.bm_step3"))
                ui.label(t("settings.bm_step4"))

            def copy_bm() -> None:
                ui.clipboard.write(bm)
                ui.notify(t("settings.bm_copied"), type="positive")

            ui.button(t("settings.bm_copy_button"), icon="content_copy", on_click=copy_bm) \
                .props("unelevated").classes("accent-grad text-white self-start")
            ui.textarea(t("settings.bm_code_label"), value=bm) \
                .props("outlined dense dark readonly autogrow").classes("w-full")
