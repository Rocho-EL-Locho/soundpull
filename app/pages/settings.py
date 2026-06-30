"""Settings page: per-user defaults, WebDAV target, and the bookmarklet."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select

from nicegui import run, ui

from app.auth import get_current_user
from app.config import settings as app_settings
from app.db import session_scope
from app.genres import ALLOWED_GENRES
from app.models import UserSettings
from app.security import decrypt_secret, encrypt_secret
from app.theme import frame
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
                "wd_url": us.webdav_url or "", "wd_user": us.webdav_username or "",
                "wd_folder": us.webdav_folder or "", "has_pw": us.has_webdav_password,
            }

        with ui.card().classes("glass w-full rounded-2xl p-6 gap-4"):
            ui.label("Profil & Standardwerte").classes("text-xl font-semibold accent-text")
            with ui.row().classes("w-full gap-3 items-end"):
                genre_sel = ui.select(ALLOWED_GENRES, value=snap["genre"], label="Standard-Genre") \
                    .props("outlined dense dark").classes("flex-1 min-w-32")
                with ui.column().classes("gap-1"):
                    ui.label("Standard-Modus").classes("text-xs text-white/50")
                    mode_tgl = ui.toggle({"album": "Album", "single": "Single"}, value=snap["mode"]) \
                        .props("toggle-color=primary unelevated no-caps").classes("glass rounded-lg")
            dest_sel = ui.select({"browser": "Im Browser (ZIP)", "webdav": "WebDAV"},
                                 value=snap["dest"], label="Standard-Ziel") \
                .props("outlined dense dark").classes("w-full")

        with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
            ui.label("WebDAV-Ziel").classes("text-lg font-semibold")
            ui.label("Basis-URL + Zugangsdaten eingeben, dann verbinden und einen Zielordner "
                     "auswählen. Das Passwort wird verschlüsselt gespeichert.") \
                .classes("text-xs text-white/50")
            wd_url = ui.input("WebDAV-URL (Basis)", value=snap["wd_url"],
                              placeholder="https://cloud.example.org/remote.php/dav/files/<user>/") \
                .props("outlined dense dark").classes("w-full")
            wd_user = ui.input("Benutzername", value=snap["wd_user"]) \
                .props("outlined dense dark").classes("w-full")
            pw_placeholder = "•••••••• (gesetzt — leer lassen zum Behalten)" if snap["has_pw"] else "Passwort"
            wd_pass = ui.input("Passwort", password=True, placeholder=pw_placeholder) \
                .props("outlined dense dark").classes("w-full")

            folder_state = {"path": snap["wd_folder"]}

            def _folder_text() -> str:
                p = folder_state["path"]
                return f"Zielordner: /{p}" if p else "Zielordner: / (Wurzel)"

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
                    ui.label("WebDAV-Ordner wählen").classes("font-semibold")
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
                                ui.label(f"Fehler: {exc}").classes("text-red-400 text-sm")
                            return
                        with lst:
                            if cur["path"]:
                                ui.button("⬑ zurück", on_click=go_up).props("flat dense no-caps") \
                                    .classes("text-white/80")
                            if not dirs:
                                ui.label("(keine Unterordner)").classes("text-white/40 text-sm")
                            for name, full in dirs:
                                ui.button(f"📁 {name}", on_click=lambda f=full: enter(f)) \
                                    .props("flat dense no-caps align=left").classes("w-full text-white/90")

                    def choose() -> None:
                        folder_state["path"] = cur["path"]
                        folder_lbl.text = _folder_text()
                        dialog.close()
                        ui.notify(f"Ordner gewählt: /{cur['path']}", type="positive")

                    with ui.row().classes("w-full justify-end gap-2 pt-2"):
                        ui.button("Abbrechen", on_click=dialog.close).props("flat")
                        ui.button("Diesen Ordner wählen", icon="check", on_click=choose) \
                            .classes("accent-grad text-white")
                await load()
                dialog.open()

            async def browse() -> None:
                url = (wd_url.value or "").strip()
                if not url:
                    ui.notify("Bitte WebDAV-URL angeben", type="warning")
                    return
                pw = await _resolve_password()
                if not pw:
                    ui.notify("Bitte Passwort angeben", type="warning")
                    return
                client = make_client(url, (wd_user.value or "").strip(), pw)
                try:
                    await run.io_bound(client.ls, "")
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"Verbindung fehlgeschlagen: {exc}", type="negative")
                    return
                await _open_picker(client)

            with ui.row().classes("items-center gap-3 flex-wrap"):
                ui.button("Verbinden & Ordner wählen", icon="folder_open", on_click=browse) \
                    .props("unelevated").classes("accent-grad text-white")
                folder_lbl = ui.label(_folder_text()).classes("text-sm text-white/70")

            def save() -> None:
                with session_scope() as session:
                    row = session.exec(select(UserSettings).where(UserSettings.user_id == uid)).first()
                    if row is None:
                        row = UserSettings(user_id=uid)
                        session.add(row)
                    row.default_genre = genre_sel.value
                    row.default_mode = mode_tgl.value
                    row.destination_type = dest_sel.value
                    row.webdav_url = (wd_url.value or "").strip() or None
                    row.webdav_folder = folder_state["path"] or None
                    row.webdav_username = (wd_user.value or "").strip() or None
                    if (wd_pass.value or "").strip():
                        row.webdav_password_enc = encrypt_secret(wd_pass.value.strip())
                    row.updated_at = datetime.now(timezone.utc)
                    session.add(row)
                ui.notify("Einstellungen gespeichert", type="positive")

            ui.button("Speichern", icon="save", on_click=save) \
                .props("unelevated").classes("accent-grad text-white hover-glow self-end px-6")

        # Bookmarklet — dragging a javascript: link is blocked in many browsers,
        # so the reliable path is: copy the code, create a bookmark, paste as URL.
        bm = ("javascript:(()=>{window.open('" + app_settings.app_base_url +
              "/?url='+encodeURIComponent(location.href),'_blank')})()")
        with ui.card().classes("glass w-full rounded-2xl p-6 gap-3"):
            ui.label("Bookmarklet").classes("text-lg font-semibold")
            with ui.column().classes("gap-1 text-xs text-white/60"):
                ui.label("Einmalig einrichten:")
                ui.label("1. Code unten mit „Code kopieren“ kopieren.")
                ui.label("2. Neues Lesezeichen anlegen (z. B. Lesezeichen-Manager → Hinzufügen).")
                ui.label("3. Als Adresse/URL den Code einfügen, Name z. B. „YT Music laden“.")
                ui.label("4. Auf music.youtube.com das Lesezeichen klicken — öffnet diese App mit der URL.")

            def copy_bm() -> None:
                ui.clipboard.write(bm)
                ui.notify("Bookmarklet-Code kopiert", type="positive")

            ui.button("Code kopieren", icon="content_copy", on_click=copy_bm) \
                .props("unelevated").classes("accent-grad text-white self-start")
            ui.textarea("Bookmarklet-Code", value=bm) \
                .props("outlined dense dark readonly autogrow").classes("w-full")
