"""Settings page: per-user defaults, WebDAV target, and the bookmarklet."""
from __future__ import annotations

import logging
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
from app.pages._shared import run_library_task
from app.pipeline import normalize_audio_format
from app.security import decrypt_secret, encrypt_secret
from app.theme import tag_option_switches
from app.webdav_util import list_dirs, make_client

log = logging.getLogger("settings")


def settings_content() -> None:
    """Sub-page builder (mounted by the app-shell ``ui.sub_pages`` router)."""
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
            "lyrics": bool(us.fetch_synced_lyrics),
            "trash_retention": int(us.trash_retention_days or 0),
            "scan_interval": int(us.library_scan_interval_hours or 0),
            "navidrome_url": us.navidrome_base_url or "",
            # Only the "is one stored?" flag — never the plaintext cookie (issue #9).
            "has_cookie": us.has_youtube_cookies,
            # Notifications (issue #42): toggles + channel config. Secrets are exposed
            # only as "is one stored?" flags (has_ntfy_token / has_smtp_password).
            "n_new_tracks": bool(us.notify_new_tracks),
            "n_sync_error": bool(us.notify_sync_error),
            "n_dl_error": bool(us.notify_download_error),
            "n_ntfy_url": us.notify_ntfy_url or "",
            "n_has_token": us.has_ntfy_token,
            "n_webhook_url": us.notify_webhook_url or "",
            "n_email_to": us.notify_email_to or "",
            "n_email_from": us.notify_smtp_from or "",
            "n_smtp_host": us.notify_smtp_host or "",
            "n_smtp_port": int(us.notify_smtp_port or 587),
            "n_smtp_user": us.notify_smtp_user or "",
            "n_has_smtp_pw": us.has_smtp_password,
            "n_security": us.notify_smtp_security or "starttls",
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
                                      "playlist": t("common.playlist"),
                                      "artist": t("common.artist")}, value=snap["mode"]) \
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
        # Synced lyrics (issue #43): fetch `.lrc` sidecars from LRCLIB; both destinations.
        lyrics_sw = ui.switch(t("settings.lyrics_label"), value=snap["lyrics"]) \
            .props("dark").classes("text-sm mt-2")
        ui.label(t("settings.lyrics_desc")).classes("text-xs text-white/50")

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

    # Notifications (issue #42): push/webhook/e-mail alerts for background events.
    # Defined before the WebDAV card so the shared save() closure below persists them;
    # secrets (ntfy token / SMTP password) follow the same clear/keep logic as the cookie.
    with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
        ui.label(t("notify.heading")).classes("text-lg font-semibold")
        ui.label(t("notify.desc")).classes("text-xs text-white/50")
        n_new_sw = ui.switch(t("notify.event_new_tracks"), value=snap["n_new_tracks"]) \
            .props("dark").classes("text-sm")
        n_syncerr_sw = ui.switch(t("notify.event_sync_error"), value=snap["n_sync_error"]) \
            .props("dark").classes("text-sm")
        n_dlerr_sw = ui.switch(t("notify.event_download_error"), value=snap["n_dl_error"]) \
            .props("dark").classes("text-sm")

        ui.label(t("notify.ntfy_heading")).classes("text-sm font-semibold mt-2 text-white/80")
        n_ntfy_url = ui.input(t("notify.ntfy_url_label"), value=snap["n_ntfy_url"]) \
            .props("outlined dense dark").classes("w-full")
        token_ph = t("notify.ntfy_token_placeholder_set") if snap["n_has_token"] \
            else t("notify.ntfy_token_label")
        n_ntfy_token = ui.input(t("notify.ntfy_token_label"), password=True,
                                placeholder=token_ph) \
            .props("outlined dense dark autocomplete=new-password").classes("w-full")
        n_token_clear = ui.switch(t("notify.ntfy_token_clear"), value=False) \
            .props("dark").classes("text-sm")
        if not snap["n_has_token"]:
            n_token_clear.set_visibility(False)

        ui.label(t("notify.webhook_heading")).classes("text-sm font-semibold mt-2 text-white/80")
        n_webhook_url = ui.input(t("notify.webhook_url_label"), value=snap["n_webhook_url"]) \
            .props("outlined dense dark").classes("w-full")

        ui.label(t("notify.email_heading")).classes("text-sm font-semibold mt-2 text-white/80")
        with ui.row().classes("w-full gap-3 flex-wrap"):
            n_email_to = ui.input(t("notify.email_to_label"), value=snap["n_email_to"]) \
                .props("outlined dense dark").classes("flex-1 min-w-40")
            n_email_from = ui.input(t("notify.email_from_label"), value=snap["n_email_from"]) \
                .props("outlined dense dark").classes("flex-1 min-w-40")
        with ui.row().classes("w-full gap-3 flex-wrap items-end"):
            n_smtp_host = ui.input(t("notify.smtp_host_label"), value=snap["n_smtp_host"]) \
                .props("outlined dense dark").classes("flex-1 min-w-40")
            n_smtp_port = ui.number(t("notify.smtp_port_label"), value=snap["n_smtp_port"],
                                    min=1, max=65535, format="%d") \
                .props("outlined dense dark").classes("w-28")
            n_security = ui.select({"starttls": t("notify.security_starttls"),
                                    "ssl": t("notify.security_ssl"),
                                    "none": t("notify.security_none")},
                                   value=snap["n_security"], label=t("notify.security_label")) \
                .props("outlined dense dark").classes("min-w-36")
        n_smtp_user = ui.input(t("notify.smtp_user_label"), value=snap["n_smtp_user"]) \
            .props("outlined dense dark autocomplete=off").classes("w-full")
        smtp_pw_ph = t("notify.smtp_password_placeholder_set") if snap["n_has_smtp_pw"] \
            else t("notify.smtp_password_label")
        n_smtp_pw = ui.input(t("notify.smtp_password_label"), password=True,
                             placeholder=smtp_pw_ph) \
            .props("outlined dense dark autocomplete=new-password").classes("w-full")
        n_smtp_pw_clear = ui.switch(t("notify.smtp_password_clear"), value=False) \
            .props("dark").classes("text-sm")
        if not snap["n_has_smtp_pw"]:
            n_smtp_pw_clear.set_visibility(False)

        async def send_test_notification() -> None:
            from app import notifications

            def _load_and_send() -> list[str]:
                # Building the config decrypts the token / SMTP password, so it must stay
                # inside the try below — a wrong FERNET_KEY raises RuntimeError.
                with session_scope() as session:
                    row = session.exec(
                        select(UserSettings).where(UserSettings.user_id == uid)).first()
                    cfg = notifications.NotifyConfig.from_settings(row) if row else None
                return notifications.send_test(cfg) if cfg is not None else []

            ui.notify(t("notify.test_running"), type="ongoing")
            try:
                sent = await run.io_bound(_load_and_send)
            except Exception as exc:  # noqa: BLE001 - surface a misconfigured channel/secret
                ui.notify(t("notify.test_error", error=exc), type="negative")
                return
            if sent:
                ui.notify(t("notify.test_sent", channels=", ".join(sent)), type="positive")
            else:
                ui.notify(t("notify.test_none"), type="warning")

        ui.button(t("notify.test_button"), icon="notifications",
                  on_click=send_test_notification) \
            .props("outline").classes("text-white/90 self-start")

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

        def _scan_done(result) -> tuple[str, str]:
            if result is None:  # a scan for this user is already running (shared guard)
                return "warning", t("settings.scan_busy")
            added, pruned, errors = result
            # Distinguish a healthy-but-empty scan from an INCOMPLETE one: unreadable
            # sub-folders mean the index may be stale and pruning was skipped (issue #38).
            if errors:
                return "warning", t("settings.scan_incomplete", count=added, failed=len(errors))
            return "positive", t("settings.scan_done", count=added, removed=pruned)

        async def scan_server() -> None:
            from app import jobs, library_ops

            def _scan():
                # Guarded run: skips if a scheduled/other scan is already walking this library.
                result = jobs.run_scan_sync(uid)
                if result is None:
                    return None
                # Opportunistic trash purge (roadmap 01): expired trash folders are cleaned up
                # here so no separate scheduler is needed. Best-effort — never fails the scan.
                try:
                    library_ops.purge_trash(uid)
                except Exception as exc:  # noqa: BLE001
                    log.warning("opportunistic trash purge failed: %s", exc)
                return result

            await run_library_task(_scan, running_key="settings.scan_running",
                                   error_key="settings.scan_error", done=_scan_done)

        ui.button(t("settings.scan_button"), icon="cloud_sync", on_click=scan_server) \
            .props("outline").classes("text-white/90 self-start")

        # Lyrics backfill (LRCGET-style, issue #43): walk the library and drop a `.lrc`
        # next to every track that lacks one. Best-effort, WebDAV-only; runs off-thread.
        ui.label(t("settings.lyrics_backfill_desc")).classes("text-xs text-white/50 mt-2")

        def _backfill_done(result) -> tuple[str, str]:
            written, skipped, missing, errors = result
            if errors:
                return "warning", t("settings.lyrics_backfill_incomplete", written=written,
                                    failed=len(errors))
            return "positive", t("settings.lyrics_backfill_done", written=written,
                                 skipped=skipped, missing=missing)

        async def backfill_lyrics_server() -> None:
            from app import library_index

            await run_library_task(lambda: library_index.backfill_lyrics(uid),
                                   running_key="settings.lyrics_backfill_running",
                                   error_key="settings.lyrics_backfill_error",
                                   done=_backfill_done)

        ui.button(t("settings.lyrics_backfill_button"), icon="lyrics",
                  on_click=backfill_lyrics_server) \
            .props("outline").classes("text-white/90 self-start")

        # Dedup (issue #31): skip already-present tracks; reference them in playlist m3u.
        ui.label(t("settings.dedup_desc")).classes("text-xs text-white/50 mt-2")
        dedup_sw = ui.switch(t("settings.dedup_label"), value=snap["dedup"]) \
            .props("dark").classes("text-sm")

        # Trash safety net (roadmap 01): a deleted library file is first moved into a dated
        # trash folder and hard-deleted only after this many days (0 = delete immediately).
        ui.label(t("settings.trash_retention_hint")).classes("text-xs text-white/50 mt-2")
        trash_retention_num = ui.number(t("settings.trash_retention"), min=0, step=1,
                                        value=snap["trash_retention"]) \
            .props("outlined dense dark").classes("w-full")

        # Scheduled library scan (roadmap 03): a background scan keeps the index fresh.
        # 0 = off (manual scan only). Requires SYNC_ENABLED (the same scheduler thread).
        ui.label(t("settings.scan_interval_hint")).classes("text-xs text-white/50 mt-2")
        scan_interval_num = ui.number(t("settings.scan_interval"), min=0, step=1,
                                      value=snap["scan_interval"]) \
            .props("outlined dense dark").classes("w-full")

        # Navidrome deep link (roadmap 03): when set, the library page links each album to a
        # Navidrome search. Optional and API-free — just a base URL like https://music.host.
        ui.label(t("settings.navidrome_hint")).classes("text-xs text-white/50 mt-2")
        navidrome_url_in = ui.input(t("settings.navidrome_url"), value=snap["navidrome_url"],
                                    placeholder="https://music.example.org") \
            .props("outlined dense dark").classes("w-full")

        # Optional: browse the trash, restore a file or empty it (features 03/04 bring the
        # real library UI; this is the minimal management surface).
        with ui.expansion(t("settings.trash_title"), icon="delete").classes("w-full"):
            trash_list = ui.column().classes("w-full gap-1")

            async def refresh_trash() -> None:
                from app import library_ops

                trash_list.clear()
                try:
                    entries = await run.io_bound(library_ops.list_trash, uid)
                except Exception as exc:  # noqa: BLE001 - surface config/connection errors
                    with trash_list:
                        ui.label(t("settings.trash_error", error=exc)) \
                            .classes("text-red-400 text-sm")
                    return
                with trash_list:
                    if not entries:
                        ui.label(t("settings.trash_empty_state")).classes("text-white/40 text-sm")
                        return
                    for entry in entries:
                        async def _restore(e=entry) -> None:
                            from app import library_ops
                            try:
                                await run.io_bound(library_ops.restore_track, uid, e.trash_rel)
                            except Exception as exc:  # noqa: BLE001
                                ui.notify(t("settings.trash_error", error=exc), type="negative")
                                return
                            ui.notify(t("settings.trash_restored", path=e.original_rel),
                                      type="positive")
                            await refresh_trash()

                        with ui.row().classes("w-full items-center justify-between gap-2"):
                            ui.label(f"🗑 {entry.original_rel} ({entry.date})") \
                                .classes("text-sm text-white/80 truncate")
                            ui.button(t("settings.trash_restore"), icon="restore",
                                      on_click=_restore) \
                                .props("flat dense no-caps").classes("text-white/90")

            async def empty_trash() -> None:
                from app import library_ops

                try:
                    removed = await run.io_bound(
                        lambda: library_ops.purge_trash(uid, force_all=True))
                except Exception as exc:  # noqa: BLE001
                    ui.notify(t("settings.trash_error", error=exc), type="negative")
                    return
                ui.notify(t("settings.trash_emptied", count=removed), type="positive")
                await refresh_trash()

            with ui.row().classes("w-full gap-2 pt-1"):
                ui.button(t("settings.trash_refresh"), icon="refresh",
                          on_click=refresh_trash).props("flat dense no-caps") \
                    .classes("text-white/90")
                ui.button(t("settings.trash_empty"), icon="delete_forever",
                          on_click=empty_trash).props("flat dense no-caps") \
                    .classes("text-red-300")

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
                row.fetch_synced_lyrics = bool(lyrics_sw.value)
                row.trash_retention_days = max(int(trash_retention_num.value or 0), 0)
                row.library_scan_interval_hours = max(int(scan_interval_num.value or 0), 0)
                row.navidrome_base_url = (navidrome_url_in.value or "").strip()
                if (wd_pass.value or "").strip():
                    row.webdav_password_enc = encrypt_secret(wd_pass.value.strip())
                # YouTube cookie (issue #9): remove wins; else store new; else keep.
                if cookie_clear.value:
                    row.youtube_cookies_enc = None
                elif (cookie_ta.value or "").strip():
                    row.youtube_cookies_enc = encrypt_secret(cookie_ta.value.strip())
                # Notifications (issue #42): toggles + channel config.
                row.notify_new_tracks = bool(n_new_sw.value)
                row.notify_sync_error = bool(n_syncerr_sw.value)
                row.notify_download_error = bool(n_dlerr_sw.value)
                row.notify_ntfy_url = (n_ntfy_url.value or "").strip() or None
                row.notify_webhook_url = (n_webhook_url.value or "").strip() or None
                row.notify_email_to = (n_email_to.value or "").strip() or None
                row.notify_smtp_from = (n_email_from.value or "").strip() or None
                row.notify_smtp_host = (n_smtp_host.value or "").strip() or None
                row.notify_smtp_port = int(n_smtp_port.value or 587)
                row.notify_smtp_user = (n_smtp_user.value or "").strip() or None
                row.notify_smtp_security = n_security.value or "starttls"
                # Secrets: clear wins → else store new → else keep (like the cookie above).
                if n_token_clear.value:
                    row.notify_ntfy_token_enc = None
                elif (n_ntfy_token.value or "").strip():
                    row.notify_ntfy_token_enc = encrypt_secret(n_ntfy_token.value.strip())
                if n_smtp_pw_clear.value:
                    row.notify_smtp_password_enc = None
                elif (n_smtp_pw.value or "").strip():
                    row.notify_smtp_password_enc = encrypt_secret(n_smtp_pw.value.strip())
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
