"""Lightweight i18n: a per-language string catalog + a `t()` lookup.

No framework — just a `{lang: {key: text}}` dict and a small resolver. The active
language lives in `app.storage.user["lang"]` (session-scoped) and is mirrored to
`UserSettings.language` for durable per-user persistence. The app shell
(`app.theme.frame`) hydrates the session value from the DB on first render and
writes it back when the user switches languages (see `app.auth.set_user_language`).

Adding a language: add its endonym to `SUPPORTED_LANGUAGES` and a full block to
`TRANSLATIONS`. Missing keys fall back to the default language, then to the raw
key, so a partial translation degrades gracefully rather than breaking.
"""
from __future__ import annotations

from nicegui import app

DEFAULT_LANGUAGE = "de"

# Endonyms shown in the switcher — deliberately NOT translated.
SUPPORTED_LANGUAGES: dict[str, str] = {"de": "Deutsch", "en": "English"}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "de": {
        # nav / app shell
        "nav.download": "Download",
        "nav.history": "Verlauf",
        "nav.subscriptions": "Abos",
        "nav.settings": "Einstellungen",
        "nav.logout": "Abmelden",
        "nav.language": "Sprache",
        # shared values
        "common.album": "Album",
        "common.single": "Single",
        "common.playlist": "Playlist",
        "genre.none": "Kein Genre",
        "dest.browser": "Im Browser (ZIP)",
        "dest.webdav": "WebDAV",
        "dest.webdav_unconfigured": "WebDAV (nicht konfiguriert)",
        # audio quality/format select labels (keys = app.pipeline.AUDIO_FORMATS)
        "audio.mp3_320": "MP3 320 kbps · max. Kompatibilität (Standard)",
        "audio.mp3_192": "MP3 192 kbps · kompatibel & kleiner",
        "audio.original": "Original (Opus/M4A) · beste Qualität, kleinste Datei",
        # metadata field toggles (keys = app.fix_music_tags.TAG_OPTION_FIELDS)
        "meta.heading": "Metadaten-Felder",
        "meta.desc": "Wähle, welche Felder Soundpull in die Dateien schreibt. "
                     "Standard: alle an — abgeschaltete Felder werden nicht geschrieben.",
        "meta.genre": "Genre",
        "meta.album_artist": "Album-Interpret",
        "meta.cover": "Cover-Bild",
        "meta.track_number": "Titelnummer",
        "meta.feat_artist": "Feat.-Bereinigung (Titel & Interpret)",
        "meta.comments": "Kommentare",
        # download phases
        "phase.queued": "Warteschlange",
        "phase.metadata": "Metadaten",
        "phase.download": "Download",
        "phase.tags": "Tags & Cover",
        "phase.packaging": "ZIP packen",
        "phase.upload": "WebDAV-Upload",
        "phase.done": "Fertig",
        "phase.error": "Fehler",
        # download page
        "index.heading_new": "Neuer Download",
        "index.url_label": "YouTube Music URL",
        "index.genre_label": "Genre",
        "index.mode_label": "Modus",
        "index.audio_label": "Qualität / Format",
        "index.dest_label": "Ziel",
        "index.start_button": "Download starten",
        "index.active_heading": "Aktive Downloads",
        "index.no_active": "Keine aktiven Downloads.",
        "index.notify_need_url": "Bitte eine URL angeben",
        "index.notify_bad_url": "Keine gültige YouTube-(Music-)URL",
        "index.notify_started": "Download gestartet",
        "index.track": "Track {current} / {total}",
        "index.unknown_error": "Unbekannter Fehler",
        "index.completed": "Abgeschlossen ✓",
        "index.download_zip": "ZIP herunterladen",
        # history page
        "history.heading": "Verlauf",
        "history.empty": "Noch keine Downloads.",
        "history.status_done": "Fertig",
        "history.status_error": "Fehler",
        "history.status_queued": "Warteschlange",
        "history.status_running": "Läuft",
        "history.status_unknown": "?",
        # settings page
        "settings.profile_heading": "Profil & Standardwerte",
        "settings.default_genre": "Standard-Genre",
        "settings.default_mode": "Standard-Modus",
        "settings.default_audio": "Standard-Qualität / Format",
        "settings.default_dest": "Standard-Ziel",
        "settings.webdav_heading": "WebDAV-Ziel",
        "settings.webdav_desc": "Basis-URL + Zugangsdaten eingeben, dann verbinden und einen "
                                "Zielordner auswählen. Das Passwort wird verschlüsselt gespeichert.",
        "settings.webdav_url_label": "WebDAV-URL (Basis)",
        "settings.username": "Benutzername",
        "settings.password": "Passwort",
        "settings.password_placeholder_set": "•••••••• (gesetzt — leer lassen zum Behalten)",
        "settings.folder_label": "Zielordner: /{path}",
        "settings.folder_root": "Zielordner: / (Wurzel)",
        "settings.picker_heading": "WebDAV-Ordner wählen",
        "settings.picker_back": "⬑ zurück",
        "settings.picker_no_sub": "(keine Unterordner)",
        "settings.picker_error": "Fehler: {error}",
        "settings.cancel": "Abbrechen",
        "settings.picker_choose": "Diesen Ordner wählen",
        "settings.folder_chosen": "Ordner gewählt: /{path}",
        "settings.notify_need_url": "Bitte WebDAV-URL angeben",
        "settings.notify_need_pw": "Bitte Passwort angeben",
        "settings.notify_conn_failed": "Verbindung fehlgeschlagen: {error}",
        "settings.connect_button": "Verbinden & Ordner wählen",
        "settings.saved": "Einstellungen gespeichert",
        "settings.save_button": "Speichern",
        # YouTube cookie (issue #9)
        "settings.cookie_heading": "YouTube-Cookie",
        "settings.cookie_desc": "Optional: Cookie hinterlegen, damit altersbeschränkte oder "
                                "gesperrte Titel und „Bestätige, dass du kein Bot bist“-Sperren "
                                "umgangen werden. Exportiere deine cookies.txt mit einer "
                                "Browser-Erweiterung (z. B. „Get cookies.txt LOCALLY“) und füge "
                                "den Inhalt hier ein. Wird verschlüsselt gespeichert.",
        "settings.cookie_label": "cookies.txt (Inhalt einfügen)",
        "settings.cookie_placeholder_set": "•••••••• (gesetzt — leer lassen zum Behalten)",
        "settings.cookie_clear": "Gespeicherten Cookie entfernen",
        "settings.bookmarklet_heading": "Bookmarklet",
        "settings.bm_setup": "Einmalig einrichten:",
        "settings.bm_step1": "1. Code unten mit „Code kopieren“ kopieren.",
        "settings.bm_step2": "2. Neues Lesezeichen anlegen (z. B. Lesezeichen-Manager → Hinzufügen).",
        "settings.bm_step3": "3. Als Adresse/URL den Code einfügen, Name z. B. „YT Music laden“.",
        "settings.bm_step4": "4. Auf music.youtube.com das Lesezeichen klicken — öffnet diese App "
                             "mit der URL.",
        "settings.bm_copied": "Bookmarklet-Code kopiert",
        "settings.bm_copy_button": "Code kopieren",
        "settings.bm_code_label": "Bookmarklet-Code",
        # Server-Bestand einlesen (issue #21)
        "settings.scan_heading": "Server-Bestand",
        "settings.scan_desc": "Einmal den WebDAV-Zielordner durchsuchen und vorhandene "
                              "Titel erfassen. Damit erkennt der Playlist-Sync, welche "
                              "Titel schon auf dem Server liegen, und lädt nur neue.",
        "settings.scan_button": "Server einlesen",
        "settings.scan_running": "Server wird eingelesen …",
        "settings.scan_done": "{count} neue Titel erfasst, {removed} veraltete entfernt",
        "settings.scan_error": "Einlesen fehlgeschlagen: {error}",
        # Dedup (issue #31)
        "settings.dedup_label": "Bereits vorhandene Titel überspringen",
        "settings.dedup_desc": "Titel, die schon in deinem Bestand liegen, nicht erneut "
                               "herunterladen. In Playlists wird stattdessen auf die "
                               "vorhandene Datei verwiesen (kein Duplikat). Nur bei WebDAV.",
        "index.dedup_label": "Vorhandene Titel überspringen",
        "index.dedup_hint": "Nur bei Ziel „WebDAV“ verfügbar.",
        # Playlist-Abos (issue #21)
        "subs.heading_new": "Neues Playlist-Abo",
        "subs.desc": "Eine Playlist in einem Intervall automatisch synchronisieren. "
                     "Jeder Lauf lädt nur Titel, die noch nicht auf dem Server liegen.",
        "subs.no_webdav": "Für Abos muss zuerst ein WebDAV-Ziel in den Einstellungen "
                          "hinterlegt werden.",
        "subs.url_label": "Playlist-URL",
        "subs.interval_label": "Intervall",
        "subs.interval_6h": "Alle 6 Stunden",
        "subs.interval_12h": "Alle 12 Stunden",
        "subs.interval_daily": "Täglich",
        "subs.interval_weekly": "Wöchentlich",
        "subs.initial_label": "Erster Lauf",
        "subs.initial_download_all": "Jetzt alles laden",
        "subs.initial_mark_existing": "Als vorhanden markieren",
        "subs.create_button": "Abo anlegen",
        "subs.list_heading": "Meine Abos",
        "subs.empty": "Noch keine Abos.",
        "subs.every_hours": "Alle {hours} h",
        "subs.enabled": "Aktiv",
        "subs.sync_now": "Jetzt synchronisieren",
        "subs.delete": "Löschen",
        "subs.last_sync_never": "Noch nie synchronisiert",
        "subs.last_sync": "Zuletzt: {when} · {count} neu",
        "subs.status_ok": "OK",
        "subs.status_error": "Fehler",
        "subs.status_idle": "Wartet",
        "subs.status_running": "Läuft",
        "subs.notify_need_url": "Bitte eine Playlist-URL angeben",
        "subs.notify_bad_url": "Keine gültige YouTube-(Music-)URL",
        "subs.notify_created": "Abo angelegt",
        "subs.notify_deleted": "Abo gelöscht",
        "subs.notify_sync_started": "Synchronisierung gestartet",
    },
    "en": {
        # nav / app shell
        "nav.download": "Download",
        "nav.history": "History",
        "nav.subscriptions": "Subscriptions",
        "nav.settings": "Settings",
        "nav.logout": "Log out",
        "nav.language": "Language",
        # shared values
        "common.album": "Album",
        "common.single": "Single",
        "common.playlist": "Playlist",
        "genre.none": "No genre",
        "dest.browser": "In browser (ZIP)",
        "dest.webdav": "WebDAV",
        "dest.webdav_unconfigured": "WebDAV (not configured)",
        # audio quality/format select labels (keys = app.pipeline.AUDIO_FORMATS)
        "audio.mp3_320": "MP3 320 kbps · max. compatibility (default)",
        "audio.mp3_192": "MP3 192 kbps · compatible & smaller",
        "audio.original": "Original (Opus/M4A) · best quality, smallest file",
        # metadata field toggles (keys = app.fix_music_tags.TAG_OPTION_FIELDS)
        "meta.heading": "Metadata fields",
        "meta.desc": "Choose which fields Soundpull writes to the files. "
                     "Default: all on — fields you turn off are not written.",
        "meta.genre": "Genre",
        "meta.album_artist": "Album artist",
        "meta.cover": "Cover art",
        "meta.track_number": "Track number",
        "meta.feat_artist": "Feat. cleanup (title & artist)",
        "meta.comments": "Comments",
        # download phases
        "phase.queued": "Queued",
        "phase.metadata": "Metadata",
        "phase.download": "Download",
        "phase.tags": "Tags & cover",
        "phase.packaging": "Packing ZIP",
        "phase.upload": "WebDAV upload",
        "phase.done": "Done",
        "phase.error": "Error",
        # download page
        "index.heading_new": "New download",
        "index.url_label": "YouTube Music URL",
        "index.genre_label": "Genre",
        "index.mode_label": "Mode",
        "index.audio_label": "Quality / format",
        "index.dest_label": "Destination",
        "index.start_button": "Start download",
        "index.active_heading": "Active downloads",
        "index.no_active": "No active downloads.",
        "index.notify_need_url": "Please enter a URL",
        "index.notify_bad_url": "Not a valid YouTube (Music) URL",
        "index.notify_started": "Download started",
        "index.track": "Track {current} / {total}",
        "index.unknown_error": "Unknown error",
        "index.completed": "Completed ✓",
        "index.download_zip": "Download ZIP",
        # history page
        "history.heading": "History",
        "history.empty": "No downloads yet.",
        "history.status_done": "Done",
        "history.status_error": "Error",
        "history.status_queued": "Queued",
        "history.status_running": "Running",
        "history.status_unknown": "?",
        # settings page
        "settings.profile_heading": "Profile & defaults",
        "settings.default_genre": "Default genre",
        "settings.default_mode": "Default mode",
        "settings.default_audio": "Default quality / format",
        "settings.default_dest": "Default destination",
        "settings.webdav_heading": "WebDAV target",
        "settings.webdav_desc": "Enter the base URL + credentials, then connect and pick a target "
                                "folder. The password is stored encrypted.",
        "settings.webdav_url_label": "WebDAV URL (base)",
        "settings.username": "Username",
        "settings.password": "Password",
        "settings.password_placeholder_set": "•••••••• (set — leave empty to keep)",
        "settings.folder_label": "Target folder: /{path}",
        "settings.folder_root": "Target folder: / (root)",
        "settings.picker_heading": "Choose WebDAV folder",
        "settings.picker_back": "⬑ back",
        "settings.picker_no_sub": "(no subfolders)",
        "settings.picker_error": "Error: {error}",
        "settings.cancel": "Cancel",
        "settings.picker_choose": "Choose this folder",
        "settings.folder_chosen": "Folder chosen: /{path}",
        "settings.notify_need_url": "Please enter a WebDAV URL",
        "settings.notify_need_pw": "Please enter a password",
        "settings.notify_conn_failed": "Connection failed: {error}",
        "settings.connect_button": "Connect & choose folder",
        "settings.saved": "Settings saved",
        "settings.save_button": "Save",
        # YouTube cookie (issue #9)
        "settings.cookie_heading": "YouTube cookie",
        "settings.cookie_desc": "Optional: provide a cookie so age-restricted or blocked tracks "
                                "and “confirm you're not a bot” prompts are bypassed. Export your "
                                "cookies.txt with a browser extension (e.g. “Get cookies.txt "
                                "LOCALLY”) and paste its contents here. Stored encrypted.",
        "settings.cookie_label": "cookies.txt (paste contents)",
        "settings.cookie_placeholder_set": "•••••••• (set — leave empty to keep)",
        "settings.cookie_clear": "Remove stored cookie",
        "settings.bookmarklet_heading": "Bookmarklet",
        "settings.bm_setup": "One-time setup:",
        "settings.bm_step1": "1. Copy the code below with “Copy code”.",
        "settings.bm_step2": "2. Create a new bookmark (e.g. Bookmark Manager → Add).",
        "settings.bm_step3": "3. Paste the code as the address/URL, name it e.g. “Load YT Music”.",
        "settings.bm_step4": "4. On music.youtube.com, click the bookmark — it opens this app "
                             "with the URL.",
        "settings.bm_copied": "Bookmarklet code copied",
        "settings.bm_copy_button": "Copy code",
        "settings.bm_code_label": "Bookmarklet code",
        # Server library scan (issue #21)
        "settings.scan_heading": "Server library",
        "settings.scan_desc": "Scan the WebDAV target folder once and index existing "
                              "tracks. This lets playlist sync recognise which titles are "
                              "already on the server and fetch only new ones.",
        "settings.scan_button": "Scan server",
        "settings.scan_running": "Scanning server …",
        "settings.scan_done": "Indexed {count} new tracks, removed {removed} stale",
        "settings.scan_error": "Scan failed: {error}",
        # Dedup (issue #31)
        "settings.dedup_label": "Skip tracks already in my library",
        "settings.dedup_desc": "Don't re-download tracks already in your library. In "
                               "playlists the existing file is referenced instead (no "
                               "duplicate). WebDAV only.",
        "index.dedup_label": "Skip tracks I already have",
        "index.dedup_hint": "Only available with the WebDAV destination.",
        # Playlist subscriptions (issue #21)
        "subs.heading_new": "New playlist subscription",
        "subs.desc": "Automatically sync a playlist on an interval. Each run downloads "
                     "only tracks that aren't on the server yet.",
        "subs.no_webdav": "Subscriptions require a WebDAV target — configure one in "
                          "Settings first.",
        "subs.url_label": "Playlist URL",
        "subs.interval_label": "Interval",
        "subs.interval_6h": "Every 6 hours",
        "subs.interval_12h": "Every 12 hours",
        "subs.interval_daily": "Daily",
        "subs.interval_weekly": "Weekly",
        "subs.initial_label": "First run",
        "subs.initial_download_all": "Download everything now",
        "subs.initial_mark_existing": "Mark as already present",
        "subs.create_button": "Create subscription",
        "subs.list_heading": "My subscriptions",
        "subs.empty": "No subscriptions yet.",
        "subs.every_hours": "Every {hours} h",
        "subs.enabled": "Enabled",
        "subs.sync_now": "Sync now",
        "subs.delete": "Delete",
        "subs.last_sync_never": "Never synced",
        "subs.last_sync": "Last: {when} · {count} new",
        "subs.status_ok": "OK",
        "subs.status_error": "Error",
        "subs.status_idle": "Idle",
        "subs.status_running": "Running",
        "subs.notify_need_url": "Please enter a playlist URL",
        "subs.notify_bad_url": "Not a valid YouTube (Music) URL",
        "subs.notify_created": "Subscription created",
        "subs.notify_deleted": "Subscription deleted",
        "subs.notify_sync_started": "Sync started",
    },
}


def current_language() -> str:
    """Active language for this session, falling back to the default.

    Reads `app.storage.user`, which requires a request/UI context; outside one
    (e.g. at import time) it falls back to the default rather than raising.
    """
    try:
        lang = app.storage.user.get("lang")
    except Exception:  # noqa: BLE001 - no request/session context
        lang = None
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def t(key: str, /, **fmt: object) -> str:
    """Translate `key` for the active language.

    Falls back to the default language, then to the raw key. Keyword args are
    applied with `str.format`, so catalog entries may contain `{name}` slots.
    A translation that references a slot the caller didn't supply degrades to
    the unformatted template rather than raising and crashing the page.
    """
    table = TRANSLATIONS.get(current_language()) or {}
    text = table.get(key) or TRANSLATIONS[DEFAULT_LANGUAGE].get(key) or key
    if not fmt:
        return text
    try:
        return text.format(**fmt)
    except (KeyError, IndexError):
        return text


def audio_format_labels() -> dict[str, str]:
    """`{format_key: localized label}` for the audio-format selects.

    Single source of truth for ordering/keys is `app.pipeline.AUDIO_FORMATS`
    (imported lazily to avoid pulling yt-dlp into this module at import time).
    """
    from app.pipeline import AUDIO_FORMATS

    return {key: t(f"audio.{key}") for key in AUDIO_FORMATS}


def genre_options() -> dict[str, str]:
    """`{genre: label}` for the genre selects, plus a "no genre" choice (issue #21).

    The genre names come from `app.genres.ALLOWED_GENRES` (label == value); the extra
    empty-string entry lets a download/playlist opt out of forcing a genre. `t()` is
    only used for the translated "no genre" label, so this stays render-time.
    """
    from app.genres import ALLOWED_GENRES

    return {g: g for g in ALLOWED_GENRES} | {"": t("genre.none")}
