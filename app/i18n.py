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
        "nav.library": "Bibliothek",
        "nav.duplicates": "Duplikate",
        "nav.health": "Zustand",
        "nav.history": "Verlauf",
        "nav.subscriptions": "Abos",
        "nav.settings": "Einstellungen",
        "nav.logout": "Abmelden",
        "nav.language": "Sprache",
        "nav.menu": "Menü ein-/ausklappen",
        "nav.close_menu": "Menü schließen",
        "nav.selfhosted": "self-hosted",
        # footer
        "footer.tagline": "Soundpull · self-hosted · MIT-Lizenz",
        "footer.github": "GitHub",
        "footer.issues": "Issues",
        "footer.license": "Lizenz",
        # shared values
        "common.album": "Album",
        "common.single": "Single",
        "common.playlist": "Playlist",
        "common.artist": "Künstler",
        "genre.none": "Kein Genre",
        "dest.browser": "Im Browser (ZIP)",
        "dest.browser_title": "Browser-ZIP",
        "dest.browser_sub": "Direkt herunterladen",
        "dest.webdav": "WebDAV",
        "dest.webdav_sub": "Auf deinen Server",
        "dest.webdav_sub_unconfigured": "Erst in Einstellungen einrichten",
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
        "phase.lyrics": "Liedtext",
        "phase.packaging": "ZIP packen",
        "phase.upload": "WebDAV-Upload",
        "phase.done": "Fertig",
        "phase.error": "Fehler",
        # download page
        "index.heading_new": "Neuer Download",
        "index.subtitle": "Füge einen YouTube-Music-Link ein und lade ihn getaggt herunter.",
        "index.url_label": "YouTube-Music-Link",
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
        "index.album_progress": "Album {current} / {total}",
        "index.unknown_error": "Unbekannter Fehler",
        "index.completed": "Abgeschlossen ✓",
        "index.download_zip": "ZIP herunterladen",
        # In-app-Suche (roadmap 07)
        "search.label": "YouTube Music durchsuchen",
        "search.placeholder": "Song, Album, Interpret oder Playlist …",
        "search.button": "Suchen",
        "search.songs": "Songs",
        "search.albums": "Alben",
        "search.artists": "Interpreten",
        "search.playlists": "Playlists",
        "search.failed": "Suche fehlgeschlagen – bitte später erneut versuchen.",
        "search.no_results": "Keine Treffer.",
        # job warnings (issue #38) — set as keys by the worker, resolved at render time
        "jobs.index_update_failed": "Upload erfolgreich, aber die Server-Index-Aktualisierung "
                                    "ist fehlgeschlagen – diese Titel könnten beim nächsten "
                                    "Sync erneut geladen werden.",
        "jobs.seed_failed": "Der Playlist-Index konnte nicht initialisiert werden – der "
                            "nächste Sync versucht es automatisch erneut.",
        "jobs.partial_delivery": "Unvollständig: {failed} von {total} Titeln fehlen – von "
                                 "YouTube gedrosselt oder geblockt. Bitte erneut herunterladen.",
        # history page
        "history.heading": "Verlauf",
        "history.empty": "Noch keine Downloads.",
        "history.status_done": "Fertig",
        "history.status_error": "Fehler",
        "history.status_queued": "Warteschlange",
        "history.status_running": "Läuft",
        "history.status_unknown": "?",
        # history page — filter / actions / detail (issue #44)
        "history.filter_search": "Suche (Artist, Album, URL)",
        "history.filter_all": "Alle",
        "history.filter_status": "Status",
        "history.filter_from": "Von",
        "history.filter_to": "Bis",
        "history.no_results": "Keine Treffer für diese Filter.",
        "history.action_details": "Details",
        "history.action_retry": "Erneut laden",
        "history.action_delete": "Löschen",
        "history.notify_retry_started": "Download erneut gestartet.",
        "history.notify_retry_no_dest": "Kein WebDAV-Ziel im Profil hinterlegt.",
        "history.notify_retry_failed": "Erneut laden fehlgeschlagen.",
        "history.notify_deleted": "Eintrag gelöscht.",
        "history.confirm_delete_heading": "Eintrag löschen?",
        "history.confirm_delete_text": "Dieser Verlaufseintrag wird dauerhaft entfernt.",
        "history.confirm_delete_yes": "Löschen",
        "history.detail_heading": "Job-Details",
        "history.detail_status": "Status",
        "history.detail_url": "URL",
        "history.detail_mode": "Modus",
        "history.detail_dest": "Ziel",
        "history.detail_audio": "Format",
        "history.detail_genre": "Genre",
        "history.detail_artist": "Artist",
        "history.detail_album": "Album",
        "history.detail_created": "Erstellt",
        "history.detail_finished": "Beendet",
        "history.detail_tracks": "Titel",
        "history.detail_failed": "Fehlgeschlagen",
        "history.detail_error": "Fehler",
        "history.detail_warning": "Warnung",
        "history.detail_log": "Verlauf / Log",
        "history.detail_no_log": "Kein Log verfügbar.",
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
        "settings.scan_incomplete": "Einlesen unvollständig: {failed} Ordner nicht lesbar, "
                                    "{count} Titel erfasst – veraltete Einträge wurden nicht "
                                    "entfernt. Bitte erneut versuchen.",
        "settings.scan_error": "Einlesen fehlgeschlagen: {error}",
        "settings.scan_busy": "Ein Scan läuft für dich bereits – bitte kurz warten.",
        # Geplanter Scan + Navidrome-Link (roadmap 03)
        "settings.scan_interval": "Automatischer Scan (Stunden)",
        "settings.scan_interval_hint": "Den WebDAV-Bestand regelmäßig im Hintergrund einlesen, "
                                       "damit die Bibliothek aktuell bleibt. 0 = aus (nur "
                                       "manueller Scan).",
        "settings.navidrome_url": "Navidrome-Adresse",
        "settings.navidrome_hint": "Optional: Basis-URL deiner Navidrome-Instanz (z. B. "
                                   "https://music.example.org). Dann verlinkt die Bibliothek "
                                   "jedes Album zur Navidrome-Suche.",
        # Lyrics-Backfill (issue #43)
        "settings.lyrics_backfill_desc": "Die gesamte Bibliothek durchgehen und für jeden Titel "
                                         "ohne Liedtext eine .lrc-Datei von LRCLIB nachladen. "
                                         "Vorhandene .lrc bleiben unangetastet. Nur bei WebDAV.",
        "settings.lyrics_backfill_button": "Liedtexte nachladen",
        "settings.lyrics_backfill_running": "Liedtexte werden nachgeladen …",
        "settings.lyrics_backfill_done": "{written} Liedtexte geschrieben, {skipped} übersprungen, "
                                         "{missing} ohne Treffer",
        "settings.lyrics_backfill_incomplete": "Nachladen unvollständig: {failed} Ordner/Uploads "
                                               "fehlgeschlagen, {written} Liedtexte geschrieben. "
                                               "Bitte erneut versuchen.",
        "settings.lyrics_backfill_error": "Nachladen fehlgeschlagen: {error}",
        # Dedup (issue #31)
        "settings.dedup_label": "Bereits vorhandene Titel überspringen",
        "settings.dedup_desc": "Titel, die schon in deinem Bestand liegen, nicht erneut "
                               "herunterladen. In Playlists wird stattdessen auf die "
                               "vorhandene Datei verwiesen (kein Duplikat). Nur bei WebDAV.",
        "index.dedup_label": "Vorhandene Titel überspringen",
        "index.dedup_hint": "Nur bei Ziel „WebDAV“ verfügbar.",
        # Papierkorb / Datei-Operationen (roadmap 01)
        "settings.trash_retention": "Papierkorb-Aufbewahrung (Tage)",
        "settings.trash_retention_hint": "Gelöschte Titel werden zunächst in einen "
                                         "Papierkorb-Ordner verschoben und erst nach so vielen "
                                         "Tagen endgültig entfernt. 0 = sofort löschen. Nur bei "
                                         "WebDAV.",
        "settings.trash_title": "Papierkorb",
        "settings.trash_refresh": "Aktualisieren",
        "settings.trash_restore": "Wiederherstellen",
        "settings.trash_empty": "Papierkorb leeren",
        "settings.trash_empty_state": "Papierkorb ist leer.",
        "settings.trash_restored": "Wiederhergestellt: {path}",
        "settings.trash_emptied": "{count} Papierkorb-Ordner geleert",
        "settings.trash_error": "Papierkorb-Vorgang fehlgeschlagen: {error}",
        # Synchronisierter Liedtext (issue #43)
        "settings.lyrics_label": "Synchronisierten Liedtext (.lrc) laden",
        "settings.lyrics_desc": "Lädt — soweit vorhanden — synchronisierten Liedtext von "
                                "LRCLIB und legt pro Titel eine .lrc-Datei daneben ab, die "
                                "Navidrome anzeigt. Ohne Treffer wird der Titel übersprungen.",
        # Export & Backup (roadmap 17)
        "settings.export_title": "Export & Backup",
        "settings.export_desc": "Lade deine Daten als Dateien herunter (nur deine eigenen, "
                                "ohne Passwörter/Secrets) — als Versicherung und für den Umzug.",
        "settings.export_library_csv": "Bibliothek (CSV)",
        "settings.export_library_json": "Bibliothek (JSON)",
        "settings.export_history_csv": "Verlauf (CSV)",
        "settings.export_settings_json": "Einstellungen (JSON)",
        "settings.import_settings": "Einstellungen importieren",
        "settings.import_confirm_title": "Einstellungen übernehmen?",
        "settings.import_confirm_body": "{count} Feld(er) werden überschrieben.",
        "settings.import_secrets_note": "Secrets (Passwörter, Token, Cookies) werden NICHT "
                                        "importiert und müssen danach neu eingegeben werden.",
        "settings.import_done": "{applied} übernommen, {skipped} übersprungen.",
        "settings.import_error": "Import fehlgeschlagen: {error}",
        "settings.import_button": "Übernehmen",
        "index.lyrics_label": "Liedtext (.lrc) laden",
        # Bibliothek (roadmap 03)
        "library.heading": "Bibliothek",
        "library.rescan": "Neu einlesen",
        "library.search": "Suchen (Interpret, Album, Titel)",
        "library.stats": "{tracks} Titel · {artists} Interpreten · {albums} Alben",
        "library.scanned_never": "noch nicht eingelesen",
        "library.scanned_recent": "gerade eingelesen",
        "library.scanned_hours": "vor {hours} h eingelesen",
        "library.scanned_days": "vor {days} Tagen eingelesen",
        "library.artists": "Interpreten",
        "library.playlists": "Playlists",
        "library.albums": "Alben",
        "library.tracks": "Titel",
        "library.no_artists": "Keine Treffer.",
        "library.pick_artist": "Wähle links einen Interpreten.",
        "library.pick_album": "Wähle ein Album.",
        "library.empty": "Deine Bibliothek ist noch leer.",
        "library.empty_no_webdav": "Hinterlege zuerst ein WebDAV-Ziel in den Einstellungen.",
        "library.empty_scan": "Server einlesen",
        "library.delete_track": "Titel löschen",
        "library.delete_album": "Album löschen",
        "library.backfill_album": "Liedtexte nachladen",
        "library.open_navidrome": "In Navidrome öffnen",
        "library.confirm_delete_track": "Diesen Titel in den Papierkorb verschieben?",
        "library.confirm_delete_album": "Dieses Album in den Papierkorb verschieben?",
        "library.delete_yes": "Löschen",
        "library.deleted": "In den Papierkorb verschoben",
        "library.delete_error": "Löschen fehlgeschlagen: {error}",
        # Duplikat-Finder (roadmap 04)
        "duplicates.heading": "Duplikate",
        "duplicates.analyze": "Bibliothek analysieren",
        "duplicates.intro": "Findet Titel, die mehrfach in deiner Bibliothek liegen, und räumt "
                            "sie sicher auf (Papierkorb, kein endgültiges Löschen).",
        "duplicates.busy": "Es läuft bereits eine Analyse.",
        "duplicates.error": "Analyse fehlgeschlagen: {error}",
        "duplicates.done": "Analyse fertig: {exact} exakte, {probable} mögliche Gruppen.",
        "duplicates.never": "Noch keine Analyse. Starte sie oben rechts.",
        "duplicates.no_webdav": "Hinterlege zuerst ein WebDAV-Ziel in den Einstellungen.",
        "duplicates.none_found": "Keine Duplikate gefunden.",
        "duplicates.phase_queued": "In Warteschlange …",
        "duplicates.phase_scanning": "Bibliothek wird durchsucht …",
        "duplicates.phase_grouping": "Duplikate werden gruppiert …",
        "duplicates.exact_heading": "Exakt ({count})",
        "duplicates.none_exact": "Keine exakten Duplikate.",
        "duplicates.probable_heading": "Wahrscheinlich ({count})",
        "duplicates.probable_hint": "Gleicher Titel nach Entfernen von Zusätzen wie "
                                    "„(Official Video)“ – bitte einzeln prüfen.",
        "duplicates.accept_all": "Alle exakten übernehmen",
        "duplicates.resolve": "Aufräumen",
        "duplicates.resolved": "{count} Titel in den Papierkorb verschoben.",
        "duplicates.resolve_error": "Aufräumen fehlgeschlagen: {error}",
        "duplicates.confirm_title": "Diese Duplikate aufräumen?",
        "duplicates.confirm_keep": "Behalten: {path}",
        "duplicates.will_trash": "{count} Kopie(n) in den Papierkorb:",
        "duplicates.repoint_note": "Playlists, die eine entfernte Kopie referenzieren, werden "
                                   "auf die behaltene Datei umgebogen.",
        "duplicates.bulk_title": "Alle exakten Vorschläge übernehmen?",
        "duplicates.bulk_body": "{groups} Gruppen · {count} Kopien werden in den Papierkorb "
                                "verschoben.",
        "duplicates.suggested": " · Vorschlag",
        "duplicates.folder_tag": "{kind} · {count} Titel",
        "duplicates.kind_playlist": "Playlist",
        "duplicates.kind_album": "Album",
        "duplicates.kind_single": "Single",
        # Zustands-Check / Health (roadmap 05)
        "health.heading": "Zustand",
        "health.intro": "Prüft die Bibliothek auf Metadaten- und Datei-Probleme und behebt die "
                        "behebbaren mit vorhandenen Mitteln (Papierkorb, kein Datenverlust).",
        "health.run_cheap": "Schnell-Checks",
        "health.run_deep": "Tiefen-Check (25 Alben)",
        "health.busy": "Es läuft bereits eine Prüfung.",
        "health.error": "Prüfung fehlgeschlagen: {error}",
        "health.done": "Prüfung fertig: {count} Befund(e).",
        "health.never": "Noch keine Prüfung. Starte sie oben rechts.",
        "health.no_webdav": "Hinterlege zuerst ein WebDAV-Ziel in den Einstellungen.",
        "health.all_clear": "Keine Probleme gefunden.",
        "health.phase_queued": "In Warteschlange …",
        "health.phase_scanning": "Bibliothek wird durchsucht …",
        "health.phase_checking": "Alben werden geprüft …",
        "health.show_findings": "{count} Befund(e) anzeigen",
        "health.deep_progress": "{count} Alben tief geprüft",
        "health.fix": "Beheben",
        "health.fixed": "Behoben.",
        "health.fix_error": "Beheben fehlgeschlagen: {error}",
        "health.confirm_trash": "Diese Datei in den Papierkorb verschieben?",
        "health.check.lyrics_missing": "Fehlende Liedtexte (.lrc)",
        "health.check.stray_file": "Verwaiste Dateien",
        "health.check.empty_folder": "Leere Ordner",
        "health.check.junk_file": "Fremddateien",
        "health.check.year_split": "Album nach Jahr aufgeteilt",
        "health.check.cover_missing": "Fehlendes Cover",
        "health.check.genre_missing": "Fehlendes Genre",
        "health.check.album_tag_missing": "Fehlendes Album/Album-Interpret",
        "health.check.corrupt_audio": "Beschädigte Audiodatei",
        "health.check_desc.lyrics_missing": "Titel ohne passende .lrc-Datei daneben.",
        "health.check_desc.stray_file": "Übrige Thumbnails oder Download-Fragmente – können weg.",
        "health.check_desc.empty_folder": "Ordner ohne Inhalt.",
        "health.check_desc.junk_file": "Keine Audio-/Cover-/Playlist-Datei – bitte selbst prüfen.",
        "health.check_desc.year_split": "Titel eines Albums tragen verschiedene Jahre – "
                                        "wird auf das früheste vereinheitlicht.",
        "health.check_desc.cover_missing": "Titel ohne eingebettetes Cover – aus cover.jpg "
                                           "oder vorhandenem Cover im Ordner ergänzen.",
        "health.check_desc.genre_missing": "Titel ohne Genre – setzt dein Standard-Genre.",
        "health.check_desc.album_tag_missing": "Album- oder Album-Interpret-Tag fehlt (nur Hinweis).",
        "health.check_desc.corrupt_audio": "Datei besteht die Dekodierung nicht (nur Hinweis; "
                                           "am besten neu laden).",
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
        # Benachrichtigungen (issue #42) — Meldungsinhalte (off-request via translate())
        "notify.new_tracks_title": "Soundpull",
        "notify.new_tracks_body": "{playlist}: {count} neue Titel",
        "notify.error_title": "Soundpull: Job fehlgeschlagen",
        "notify.error_body": "{kind} fehlgeschlagen: {error}",
        "notify.kind_sync": "Playlist-Sync",
        "notify.kind_download": "Download",
        "notify.test_title": "Soundpull: Test",
        "notify.test_body": "Testbenachrichtigung — deine Einstellungen funktionieren. 🎵",
        # Benachrichtigungen (issue #42) — Einstellungen
        "notify.heading": "Benachrichtigungen",
        "notify.desc": "Optional bei Hintergrund-Ereignissen benachrichtigen lassen. Wähle die "
                       "Ereignisse und mindestens einen Kanal (ntfy, Webhook oder E-Mail). "
                       "Keine Passwörter oder Tokens werden im Inhalt mitgesendet.",
        "notify.event_new_tracks": "Neue Titel bei Playlist-Sync",
        "notify.event_sync_error": "Fehlgeschlagener Playlist-Sync",
        "notify.event_download_error": "Fehlgeschlagener Download",
        "notify.ntfy_heading": "ntfy (Push)",
        "notify.ntfy_url_label": "ntfy-Topic-URL (z. B. https://ntfy.sh/mein-topic)",
        "notify.ntfy_token_label": "Zugriffstoken (optional)",
        "notify.ntfy_token_placeholder_set": "•••••••• (gesetzt — leer lassen zum Behalten)",
        "notify.ntfy_token_clear": "Gespeichertes Token entfernen",
        "notify.webhook_heading": "Webhook",
        "notify.webhook_url_label": "Webhook-URL (JSON-POST)",
        "notify.email_heading": "E-Mail (SMTP)",
        "notify.email_to_label": "Empfänger (An)",
        "notify.email_from_label": "Absender (Von)",
        "notify.smtp_host_label": "SMTP-Server",
        "notify.smtp_port_label": "Port",
        "notify.smtp_user_label": "SMTP-Benutzer",
        "notify.smtp_password_label": "SMTP-Passwort",
        "notify.smtp_password_placeholder_set": "•••••••• (gesetzt — leer lassen zum Behalten)",
        "notify.smtp_password_clear": "Gespeichertes SMTP-Passwort entfernen",
        "notify.security_label": "Verschlüsselung",
        "notify.security_starttls": "STARTTLS",
        "notify.security_ssl": "SSL/TLS",
        "notify.security_none": "Keine",
        "notify.test_button": "Test senden",
        "notify.test_running": "Testbenachrichtigung wird gesendet …",
        "notify.test_sent": "Testbenachrichtigung gesendet an: {channels}",
        "notify.test_none": "Kein Kanal konfiguriert — bitte zuerst speichern.",
        "notify.test_error": "Test fehlgeschlagen: {error}",
    },
    "en": {
        # nav / app shell
        "nav.download": "Download",
        "nav.library": "Library",
        "nav.duplicates": "Duplicates",
        "nav.health": "Health",
        "nav.history": "History",
        "nav.subscriptions": "Subscriptions",
        "nav.settings": "Settings",
        "nav.logout": "Log out",
        "nav.language": "Language",
        "nav.menu": "Toggle menu",
        "nav.close_menu": "Close menu",
        "nav.selfhosted": "self-hosted",
        # footer
        "footer.tagline": "Soundpull · self-hosted · MIT license",
        "footer.github": "GitHub",
        "footer.issues": "Issues",
        "footer.license": "License",
        # shared values
        "common.album": "Album",
        "common.single": "Single",
        "common.playlist": "Playlist",
        "common.artist": "Artist",
        "genre.none": "No genre",
        "dest.browser": "In browser (ZIP)",
        "dest.browser_title": "Browser ZIP",
        "dest.browser_sub": "Download directly",
        "dest.webdav": "WebDAV",
        "dest.webdav_sub": "To your server",
        "dest.webdav_sub_unconfigured": "Set up in Settings first",
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
        "phase.lyrics": "Lyrics",
        "phase.packaging": "Packing ZIP",
        "phase.upload": "WebDAV upload",
        "phase.done": "Done",
        "phase.error": "Error",
        # download page
        "index.heading_new": "New download",
        "index.subtitle": "Paste a YouTube Music link and download it fully tagged.",
        "index.url_label": "YouTube Music link",
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
        "index.album_progress": "Album {current} / {total}",
        "index.unknown_error": "Unknown error",
        "index.completed": "Completed ✓",
        "index.download_zip": "Download ZIP",
        # In-app search (roadmap 07)
        "search.label": "Search YouTube Music",
        "search.placeholder": "Song, album, artist or playlist …",
        "search.button": "Search",
        "search.songs": "Songs",
        "search.albums": "Albums",
        "search.artists": "Artists",
        "search.playlists": "Playlists",
        "search.failed": "Search failed — please try again later.",
        "search.no_results": "No results.",
        # job warnings (issue #38) — set as keys by the worker, resolved at render time
        "jobs.index_update_failed": "Upload succeeded, but updating the server index failed — "
                                    "these tracks may be downloaded again on the next sync.",
        "jobs.partial_delivery": "Incomplete: {failed} of {total} tracks are missing — "
                                 "throttled or blocked by YouTube. Please download again.",
        "jobs.seed_failed": "The playlist index could not be initialised — the next sync will "
                            "automatically try again.",
        # history page
        "history.heading": "History",
        "history.empty": "No downloads yet.",
        "history.status_done": "Done",
        "history.status_error": "Error",
        "history.status_queued": "Queued",
        "history.status_running": "Running",
        "history.status_unknown": "?",
        # history page — filter / actions / detail (issue #44)
        "history.filter_search": "Search (artist, album, URL)",
        "history.filter_all": "All",
        "history.filter_status": "Status",
        "history.filter_from": "From",
        "history.filter_to": "To",
        "history.no_results": "No matches for these filters.",
        "history.action_details": "Details",
        "history.action_retry": "Download again",
        "history.action_delete": "Delete",
        "history.notify_retry_started": "Download restarted.",
        "history.notify_retry_no_dest": "No WebDAV target configured in your profile.",
        "history.notify_retry_failed": "Retry failed.",
        "history.notify_deleted": "Entry deleted.",
        "history.confirm_delete_heading": "Delete entry?",
        "history.confirm_delete_text": "This history entry will be permanently removed.",
        "history.confirm_delete_yes": "Delete",
        "history.detail_heading": "Job details",
        "history.detail_status": "Status",
        "history.detail_url": "URL",
        "history.detail_mode": "Mode",
        "history.detail_dest": "Destination",
        "history.detail_audio": "Format",
        "history.detail_genre": "Genre",
        "history.detail_artist": "Artist",
        "history.detail_album": "Album",
        "history.detail_created": "Created",
        "history.detail_finished": "Finished",
        "history.detail_tracks": "Tracks",
        "history.detail_failed": "Failed",
        "history.detail_error": "Error",
        "history.detail_warning": "Warning",
        "history.detail_log": "Timeline / log",
        "history.detail_no_log": "No log available.",
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
        "settings.scan_incomplete": "Scan incomplete: {failed} folder(s) unreadable, "
                                    "{count} tracks indexed — stale entries were not pruned. "
                                    "Please try again.",
        "settings.scan_error": "Scan failed: {error}",
        "settings.scan_busy": "A scan is already running for you — please wait a moment.",
        # Scheduled scan + Navidrome link (roadmap 03)
        "settings.scan_interval": "Automatic scan (hours)",
        "settings.scan_interval_hint": "Periodically scan the WebDAV library in the background "
                                       "so the index stays fresh. 0 = off (manual scan only).",
        "settings.navidrome_url": "Navidrome address",
        "settings.navidrome_hint": "Optional: base URL of your Navidrome instance (e.g. "
                                   "https://music.example.org). The library then links each "
                                   "album to a Navidrome search.",
        # Lyrics backfill (issue #43)
        "settings.lyrics_backfill_desc": "Walk the whole library and fetch a .lrc from LRCLIB "
                                         "for every track that has no lyrics yet. Existing .lrc "
                                         "files are left untouched. WebDAV only.",
        "settings.lyrics_backfill_button": "Backfill lyrics",
        "settings.lyrics_backfill_running": "Backfilling lyrics …",
        "settings.lyrics_backfill_done": "Wrote {written} lyrics, skipped {skipped}, "
                                         "{missing} with no match",
        "settings.lyrics_backfill_incomplete": "Backfill incomplete: {failed} folder(s)/uploads "
                                               "failed, wrote {written} lyrics. Please try again.",
        "settings.lyrics_backfill_error": "Backfill failed: {error}",
        # Dedup (issue #31)
        "settings.dedup_label": "Skip tracks already in my library",
        "settings.dedup_desc": "Don't re-download tracks already in your library. In "
                               "playlists the existing file is referenced instead (no "
                               "duplicate). WebDAV only.",
        "index.dedup_label": "Skip tracks I already have",
        "index.dedup_hint": "Only available with the WebDAV destination.",
        # Trash / file operations (roadmap 01)
        "settings.trash_retention": "Trash retention (days)",
        "settings.trash_retention_hint": "Deleted tracks are first moved to a trash folder and "
                                         "only permanently removed after this many days. "
                                         "0 = delete immediately. WebDAV only.",
        "settings.trash_title": "Trash",
        "settings.trash_refresh": "Refresh",
        "settings.trash_restore": "Restore",
        "settings.trash_empty": "Empty trash",
        "settings.trash_empty_state": "Trash is empty.",
        "settings.trash_restored": "Restored: {path}",
        "settings.trash_emptied": "Emptied {count} trash folder(s)",
        "settings.trash_error": "Trash operation failed: {error}",
        # Synced lyrics (issue #43)
        "settings.lyrics_label": "Fetch synced lyrics (.lrc)",
        "settings.lyrics_desc": "When available, fetch synced lyrics from LRCLIB and drop a "
                                ".lrc file next to each track for Navidrome to display. Tracks "
                                "with no match are simply skipped.",
        # Export & backup (roadmap 17)
        "settings.export_title": "Export & backup",
        "settings.export_desc": "Download your data as files (your own only, without "
                                "passwords/secrets) — as insurance and for migration.",
        "settings.export_library_csv": "Library (CSV)",
        "settings.export_library_json": "Library (JSON)",
        "settings.export_history_csv": "History (CSV)",
        "settings.export_settings_json": "Settings (JSON)",
        "settings.import_settings": "Import settings",
        "settings.import_confirm_title": "Apply settings?",
        "settings.import_confirm_body": "{count} field(s) will be overwritten.",
        "settings.import_secrets_note": "Secrets (passwords, tokens, cookies) are NOT imported "
                                        "and must be re-entered afterwards.",
        "settings.import_done": "{applied} applied, {skipped} skipped.",
        "settings.import_error": "Import failed: {error}",
        "settings.import_button": "Apply",
        "index.lyrics_label": "Fetch lyrics (.lrc)",
        # Library (roadmap 03)
        "library.heading": "Library",
        "library.rescan": "Rescan",
        "library.search": "Search (artist, album, title)",
        "library.stats": "{tracks} tracks · {artists} artists · {albums} albums",
        "library.scanned_never": "not scanned yet",
        "library.scanned_recent": "scanned just now",
        "library.scanned_hours": "scanned {hours}h ago",
        "library.scanned_days": "scanned {days}d ago",
        "library.artists": "Artists",
        "library.playlists": "Playlists",
        "library.albums": "Albums",
        "library.tracks": "Tracks",
        "library.no_artists": "No matches.",
        "library.pick_artist": "Pick an artist on the left.",
        "library.pick_album": "Pick an album.",
        "library.empty": "Your library is still empty.",
        "library.empty_no_webdav": "Configure a WebDAV target in settings first.",
        "library.empty_scan": "Scan server",
        "library.delete_track": "Delete track",
        "library.delete_album": "Delete album",
        "library.backfill_album": "Backfill lyrics",
        "library.open_navidrome": "Open in Navidrome",
        "library.confirm_delete_track": "Move this track to the trash?",
        "library.confirm_delete_album": "Move this album to the trash?",
        "library.delete_yes": "Delete",
        "library.deleted": "Moved to trash",
        "library.delete_error": "Delete failed: {error}",
        # Duplicate finder (roadmap 04)
        "duplicates.heading": "Duplicates",
        "duplicates.analyze": "Analyze library",
        "duplicates.intro": "Finds tracks that sit in your library more than once and cleans "
                            "them up safely (trash, never a hard delete).",
        "duplicates.busy": "An analysis is already running.",
        "duplicates.error": "Analysis failed: {error}",
        "duplicates.done": "Analysis complete: {exact} exact, {probable} probable groups.",
        "duplicates.never": "No analysis yet. Start one from the top right.",
        "duplicates.no_webdav": "Set a WebDAV target in the settings first.",
        "duplicates.none_found": "No duplicates found.",
        "duplicates.phase_queued": "Queued …",
        "duplicates.phase_scanning": "Scanning the library …",
        "duplicates.phase_grouping": "Grouping duplicates …",
        "duplicates.exact_heading": "Exact ({count})",
        "duplicates.none_exact": "No exact duplicates.",
        "duplicates.probable_heading": "Probable ({count})",
        "duplicates.probable_hint": "Same title after stripping noise like “(Official Video)” "
                                    "— please review individually.",
        "duplicates.accept_all": "Accept all exact",
        "duplicates.resolve": "Clean up",
        "duplicates.resolved": "Moved {count} track(s) to the trash.",
        "duplicates.resolve_error": "Cleanup failed: {error}",
        "duplicates.confirm_title": "Clean up these duplicates?",
        "duplicates.confirm_keep": "Keeping: {path}",
        "duplicates.will_trash": "{count} copy/copies to the trash:",
        "duplicates.repoint_note": "Playlists referencing a removed copy are re-pointed at the "
                                   "kept file.",
        "duplicates.bulk_title": "Accept all exact suggestions?",
        "duplicates.bulk_body": "{groups} groups · {count} copies will be moved to the trash.",
        "duplicates.suggested": " · suggested",
        "duplicates.folder_tag": "{kind} · {count} tracks",
        "duplicates.kind_playlist": "playlist",
        "duplicates.kind_album": "album",
        "duplicates.kind_single": "single",
        # Library health check (roadmap 05)
        "health.heading": "Health",
        "health.intro": "Audits the library for metadata/file problems and fixes the fixable "
                        "ones with existing machinery (trash, never a hard delete).",
        "health.run_cheap": "Quick checks",
        "health.run_deep": "Deep check (25 albums)",
        "health.busy": "A check is already running.",
        "health.error": "Check failed: {error}",
        "health.done": "Check complete: {count} finding(s).",
        "health.never": "No check yet. Start one from the top right.",
        "health.no_webdav": "Set a WebDAV target in the settings first.",
        "health.all_clear": "No problems found.",
        "health.phase_queued": "Queued …",
        "health.phase_scanning": "Scanning the library …",
        "health.phase_checking": "Checking albums …",
        "health.show_findings": "Show {count} finding(s)",
        "health.deep_progress": "{count} albums deep-checked",
        "health.fix": "Fix",
        "health.fixed": "Fixed.",
        "health.fix_error": "Fix failed: {error}",
        "health.confirm_trash": "Move this file to the trash?",
        "health.check.lyrics_missing": "Missing lyrics (.lrc)",
        "health.check.stray_file": "Stray files",
        "health.check.empty_folder": "Empty folders",
        "health.check.junk_file": "Foreign files",
        "health.check.year_split": "Album split by year",
        "health.check.cover_missing": "Missing cover",
        "health.check.genre_missing": "Missing genre",
        "health.check.album_tag_missing": "Missing album/album-artist",
        "health.check.corrupt_audio": "Corrupt audio file",
        "health.check_desc.lyrics_missing": "Tracks with no sibling .lrc file.",
        "health.check_desc.stray_file": "Leftover thumbnails or download fragments — safe to remove.",
        "health.check_desc.empty_folder": "Folders with no content.",
        "health.check_desc.junk_file": "Not an audio/cover/playlist file — please review yourself.",
        "health.check_desc.year_split": "An album's tracks carry different years — unified to the "
                                        "earliest.",
        "health.check_desc.cover_missing": "Tracks without embedded cover — filled from cover.jpg "
                                           "or existing in-folder art.",
        "health.check_desc.genre_missing": "Tracks without a genre — writes your default genre.",
        "health.check_desc.album_tag_missing": "Album or album-artist tag missing (report only).",
        "health.check_desc.corrupt_audio": "File fails to decode (report only; re-download is best).",
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
        # Notifications (issue #42) — message content (off-request via translate())
        "notify.new_tracks_title": "Soundpull",
        "notify.new_tracks_body": "{playlist}: {count} new tracks",
        "notify.error_title": "Soundpull: job failed",
        "notify.error_body": "{kind} failed: {error}",
        "notify.kind_sync": "Playlist sync",
        "notify.kind_download": "Download",
        "notify.test_title": "Soundpull: test",
        "notify.test_body": "Test notification — your settings work. 🎵",
        # Notifications (issue #42) — settings
        "notify.heading": "Notifications",
        "notify.desc": "Optionally get notified about background events. Pick the events and at "
                       "least one channel (ntfy, webhook or e-mail). No passwords or tokens are "
                       "ever included in the payload.",
        "notify.event_new_tracks": "New tracks on playlist sync",
        "notify.event_sync_error": "Failed playlist sync",
        "notify.event_download_error": "Failed download",
        "notify.ntfy_heading": "ntfy (push)",
        "notify.ntfy_url_label": "ntfy topic URL (e.g. https://ntfy.sh/my-topic)",
        "notify.ntfy_token_label": "Access token (optional)",
        "notify.ntfy_token_placeholder_set": "•••••••• (set — leave empty to keep)",
        "notify.ntfy_token_clear": "Remove stored token",
        "notify.webhook_heading": "Webhook",
        "notify.webhook_url_label": "Webhook URL (JSON POST)",
        "notify.email_heading": "E-mail (SMTP)",
        "notify.email_to_label": "Recipient (To)",
        "notify.email_from_label": "Sender (From)",
        "notify.smtp_host_label": "SMTP server",
        "notify.smtp_port_label": "Port",
        "notify.smtp_user_label": "SMTP username",
        "notify.smtp_password_label": "SMTP password",
        "notify.smtp_password_placeholder_set": "•••••••• (set — leave empty to keep)",
        "notify.smtp_password_clear": "Remove stored SMTP password",
        "notify.security_label": "Encryption",
        "notify.security_starttls": "STARTTLS",
        "notify.security_ssl": "SSL/TLS",
        "notify.security_none": "None",
        "notify.test_button": "Send test",
        "notify.test_running": "Sending test notification …",
        "notify.test_sent": "Test notification sent to: {channels}",
        "notify.test_none": "No channel configured — please save first.",
        "notify.test_error": "Test failed: {error}",
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


def translate(lang: str, key: str, /, **fmt: object) -> str:
    """Translate `key` for an EXPLICIT language.

    Same fallback/format behaviour as `t()`, but the language is passed in rather
    than read from the session. Used off-request (e.g. the notification worker in
    `app.notifications`, issue #42), where there is no session to read the active
    language from — so notification strings still live in this catalog.
    """
    lang = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    table = TRANSLATIONS.get(lang) or {}
    text = table.get(key) or TRANSLATIONS[DEFAULT_LANGUAGE].get(key) or key
    if not fmt:
        return text
    try:
        return text.format(**fmt)
    except (KeyError, IndexError):
        return text


def t(key: str, /, **fmt: object) -> str:
    """Translate `key` for the active language.

    Falls back to the default language, then to the raw key. Keyword args are
    applied with `str.format`, so catalog entries may contain `{name}` slots.
    A translation that references a slot the caller didn't supply degrades to
    the unformatted template rather than raising and crashing the page.
    """
    return translate(current_language(), key, **fmt)


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
