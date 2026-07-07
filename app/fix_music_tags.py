#!/usr/bin/env python3
"""
fix_music_tags.py — Korrigiert Featured Artist Metadaten in Musikdateien

Navidrome-konforme Regeln:
  - ARTIST:      "Primärkünstler / Featured Artist"  (Trennzeichen: " / ")
  - ALBUMARTIST: Nur Primärkünstler (unverändert)
  - TITLE:       Ohne "(feat. ...)" Anteil

Die Normalisierungs-Regeln (parse_featured_artists / split_artists / FEAT_PATTERNS)
sind unverändert vom Original übernommen und gelten für alle Formate gleich. Der
MP3-Pfad (process_file → ID3v2.3) ist bit-identisch zum Original; M4A/MP4 und
Opus/OGG werden zusätzlich unterstützt, damit auch der "Original-Codec"-Download
(yt-dlp ohne Re-Encode, siehe issue #10) die volle Navidrome-Behandlung erhält.

Aufruf: python3 fix_music_tags.py <Verzeichnis>
"""

import base64
import os
import re
import sys
from dataclasses import dataclass

from mutagen.flac import Picture
from mutagen.id3 import ID3, TPE1, TPE2, TIT2, TALB, APIC, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis


@dataclass(frozen=True)
class TagOptions:
    """Welche Metadaten-Felder Soundpull schreibt (issue #7).

    Alle Schalter an (Default) = ursprüngliches Verhalten, byte-identisch zur
    Parität. Ein Schalter aus → das Feld wird NICHT geschrieben und ein evtl. von
    yt-dlp eingebetteter Wert wird beim Tagging wieder entfernt (siehe _strip_*).
    `feat_artist` ist die Ausnahme: aus = die Titel-/Artist-Bereinigung wird
    übersprungen, die rohen yt-dlp-Werte bleiben stehen.
    """
    genre: bool = True
    album_artist: bool = True
    cover: bool = True
    track_number: bool = True
    feat_artist: bool = True
    comments: bool = True


# Stabile Feldreihenfolge für UI/Tests (entspricht den TagOptions-Attributen).
TAG_OPTION_FIELDS = ("genre", "album_artist", "cover", "track_number", "feat_artist", "comments")

# Default-Instanz: TagOptions ist frozen → als geteilter Default-Parameter sicher.
_ALL_ON = TagOptions()

# TXXX-Beschreibungen (lowercase), die yt-dlp/ffmpeg als „Kommentar“-artige Felder
# schreibt (das eigentliche COMM trägt die Webseiten-URL).
_COMMENT_TXXX_DESCS = {"description", "synopsis", "purl", "comment"}


# Regex-Muster für Featured Artists im Titel
FEAT_PATTERNS = [
    # (feat. Artist B), [feat. Artist B], (ft. Artist B), etc.
    r'\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)\s*[\)\]]',
    # "Song feat. Artist B" am Ende oder vor einem Klammerausdruck
    r'\s+(?:feat\.?|ft\.?|featuring)\s+([^(\[]+?)(?:\s*[\(\[]|$)',
]


def split_artists(feat_string: str) -> list[str]:
    """Splittet mehrere Featured Artists: 'A & B, C' → ['A', 'B', 'C']"""
    # Trennzeichen: &, ,, und
    parts = re.split(r'\s*(?:&|,)\s*|\s+und\s+', feat_string)
    return [p.strip() for p in parts if p.strip()]


# Ein feat./ft./featuring-Marker DIREKT im Artist-Tag (nicht im Titel), z.B.
# "A feat. B & C". Nur der feat-Marker trennt Primär- von Feature-Künstlern —
# absichtlich NICHT '&'/' x '/' und ', denn die sind auch in echten Bandnamen
# gebräuchlich ("Simon & Garfunkel", "Earth, Wind & Fire", "Malcolm X").
_ARTIST_FEAT_RE = re.compile(r'\s+(?:feat\.?|ft\.?|featuring)\s+', re.IGNORECASE)


def _split_artist_feat(artist: str) -> tuple[str, list[str]]:
    """Trennt einen Artist-Tag an einem eingebetteten feat.-Marker.

    "A feat. B & C" → ("A", ["B", "C"]). Ohne Marker → (artist, []). '&'/','/'und'
    werden NUR im Feature-Teil aufgesplittet (via `split_artists`), damit ein echter
    Bandname im Primärteil erhalten bleibt.
    """
    m = _ARTIST_FEAT_RE.search(artist)
    if not m:
        return artist, []
    return artist[:m.start()].strip(), split_artists(artist[m.end():])


def parse_featured_artists(title: str, artist: str) -> tuple[str, str, str]:
    """
    Extrahiert Featured Artists aus dem Titel und normalisiert den Artist-Tag.

    yt-dlp schreibt bei feat.-Tracks den Artist-Tag als komma-separierte Liste,
    z.B. "Joey Valence & Brae, Danny Brown". Diese Funktion:
      1. Extrahiert Featured Artists aus dem Titel
      2. Bestimmt den Primärkünstler (erster Eintrag vor dem ersten ", ")
      3. Baut "Primärkünstler / Feat1 / Feat2" (Navidrome-Format)
      4. Setzt AlbumArtist auf nur den Primärkünstler

    Returns:
        (clean_title, new_artist, album_artist)
    """
    clean_title = title
    featured_from_title = []

    for pattern in FEAT_PATTERNS:
        match = re.search(pattern, clean_title, re.IGNORECASE)
        if match:
            feat_string = match.group(1).strip()
            featured_from_title = split_artists(feat_string)
            clean_title = re.sub(pattern, '', clean_title, flags=re.IGNORECASE).strip()
            break

    # Ein feat.-Marker kann auch DIREKT im Artist-Tag stehen (z.B. "A feat. B"), nicht
    # nur im Titel. Den normalisieren wir ebenfalls zu " / ". Für einen reinen Komma-Tag
    # ("A, B") ist `artist_primary == artist` und `featured_from_artist == []`, also bleibt
    # der eingefrorene Pfad unten unverändert.
    artist_primary, featured_from_artist = _split_artist_feat(artist)

    if not featured_from_title and not featured_from_artist:
        # Kein feat. (weder im Titel noch im Artist-Tag) → nur Komma-Trennung normalisieren
        if ", " in artist:
            parts = [a.strip() for a in artist.split(", ")]
            return title, " / ".join(parts), parts[0]
        return title, artist, artist

    # yt-dlp schreibt Artist-Tag oft als "Primär, Feat1, Feat2" (komma-separiert) ODER
    # "Primär feat. Feat1 & Feat2". Feat-Marker ist bereits abgetrennt (`artist_primary`),
    # bleibt die Komma-Liste. Primärkünstler = Teile, die NICHT featured sind.
    artist_parts = [a.strip() for a in artist_primary.split(", ")]

    # Featured aus Titel UND Artist-Tag zusammenführen (Reihenfolge erhalten, ci-dedupe),
    # sonst landet ein doppelt genannter Feature-Artist zweimal im Tag ("A / B / B").
    featured = list(featured_from_title)
    seen = {f.lower() for f in featured}
    for f in featured_from_artist:
        if f.lower() not in seen:
            seen.add(f.lower())
            featured.append(f)

    feat_lower = {f.lower() for f in featured}
    primary_parts = [p for p in artist_parts if p.lower() not in feat_lower]
    primary_artist = ", ".join(primary_parts) if primary_parts else artist_parts[0]

    # Finaler Artist-Tag: Primärkünstler / Feat1 / Feat2
    new_artist = " / ".join([primary_artist] + featured)

    return clean_title, new_artist, primary_artist


def get_tag_text(tags, key: str) -> str:
    """Liest einen ID3-Tag sicher als String."""
    frame = tags.get(key)
    if frame is None:
        return ""
    return str(frame).strip()


def _strip_id3(tags: ID3, options: TagOptions) -> bool:
    """Entfernt ID3-Frames für abgeschaltete Felder (issue #7). True wenn etwas wegfiel."""
    changed = False

    def drop(label: str, *keys: str) -> None:
        nonlocal changed
        hit = any(tags.getall(k) for k in keys)
        if hit:
            for k in keys:
                tags.delall(k)
            print(f"  [STRIP] {label}")
            changed = True

    if not options.genre:
        drop("Genre", "TCON")
    if not options.album_artist:
        drop("AlbumArtist", "TPE2")
    if not options.track_number:
        drop("Track", "TRCK")
    if not options.cover:
        drop("Cover", "APIC")
    if not options.comments:
        hit = bool(tags.getall("COMM"))
        tags.delall("COMM")
        for frame in list(tags.getall("TXXX")):
            if frame.desc.lower() in _COMMENT_TXXX_DESCS:
                del tags[frame.HashKey]
                hit = True
        if hit:
            print("  [STRIP] Kommentare")
            changed = True

    return changed


def _strip_dict(audio, options: TagOptions, fields: dict[str, tuple[str, ...]]) -> bool:
    """Strip für dict-artige Tags (MP4-Atome / Vorbis-Comments). True wenn etwas wegfiel.

    `fields` mappt einen TagOptions-Attributnamen auf die zu löschenden Schlüssel.
    Gemeinsame Logik für MP4 und Opus/OGG (gleiche Lösch-API: `key in audio` / del).
    """
    changed = False
    labels = {"genre": "Genre", "album_artist": "AlbumArtist", "track_number": "Track",
              "cover": "Cover", "comments": "Kommentare"}
    for attr, keys in fields.items():
        if getattr(options, attr):
            continue
        hit = False
        for key in keys:
            if key in audio:
                del audio[key]
                hit = True
        if hit:
            print(f"  [STRIP] {labels[attr]}")
            changed = True
    return changed


_MP4_STRIP_KEYS = {
    "genre": ("\xa9gen", "gnre"),
    "album_artist": ("aART",),
    "track_number": ("trkn",),
    "cover": ("covr",),
    "comments": ("\xa9cmt", "desc", "ldes"),
}
_VORBIS_STRIP_KEYS = {
    "genre": ("genre",),
    "album_artist": ("albumartist",),
    "track_number": ("tracknumber",),
    "cover": ("metadata_block_picture", "coverart"),
    "comments": ("comment", "description", "synopsis", "purl"),
}


def process_file(filepath: str, cover_data: bytes | None = None, album_name: str | None = None, album_artist: str | None = None, options: TagOptions = _ALL_ON) -> bool:
    """
    Verarbeitet eine einzelne MP3-Datei.
    cover_data: optionale JPG-Bytes für das Cover-Bild (ersetzt das eingebettete Thumbnail)
    options: welche Felder geschrieben werden (issue #7); Default = alle an (Parität).
    Returns True wenn Änderungen vorgenommen wurden.
    """
    try:
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            print(f"  [SKIP] Keine ID3-Tags: {os.path.basename(filepath)}")
            return False

        title = get_tag_text(tags, "TIT2")
        artist = get_tag_text(tags, "TPE1")
        albumartist = get_tag_text(tags, "TPE2")

        if not title:
            print(f"  [SKIP] Kein Titel-Tag: {os.path.basename(filepath)}")
            return False

        if not artist:
            print(f"  [SKIP] Kein Artist-Tag: {os.path.basename(filepath)}")
            return False

        clean_title, new_artist, _ = parse_featured_artists(title, artist)

        changed = False

        # Feat.-Bereinigung von Titel & Artist (nur wenn aktiviert)
        if options.feat_artist:
            if clean_title != title:
                print(f"  Titel:       '{title}' → '{clean_title}'")
                tags["TIT2"] = TIT2(encoding=3, text=clean_title)
                changed = True
            if new_artist != artist:
                print(f"  Artist:      '{artist}' → '{new_artist}'")
                tags["TPE1"] = TPE1(encoding=3, text=new_artist)
                changed = True

        # AlbumArtist: auf den korrekten Primärkünstler setzen (nur wenn aktiviert).
        # Priorität: explizit übergebener album_artist > erster Teil des Artists, der
        # auch tatsächlich geschrieben wird (bei feat-Bereinigung `new_artist`, sonst
        # der rohe `artist` — sonst driften MP3 und M4A/Opus auseinander). Der
        # Pipeline-Pfad (Album/Single) übergibt IMMER einen album_artist, daher ist
        # der Fallback dort nie aktiv → Parität bleibt bit-identisch. Er greift nur
        # ohne expliziten Wert (Playlist-Tagging via process_tree, issue #11) und
        # stimmt so mit dem M4A/Opus-Pfad (_normalized_tags) überein.
        if options.album_artist:
            effective_artist = new_artist if options.feat_artist else artist
            correct_albumartist = album_artist if album_artist else effective_artist.split(" / ")[0]
            if albumartist != correct_albumartist:
                label = "(leer)" if not albumartist else f"'{albumartist}'"
                print(f"  AlbumArtist: {label} → '{correct_albumartist}'")
                tags["TPE2"] = TPE2(encoding=3, text=correct_albumartist)
                changed = True

        # Album-Name: expliziten Wert übernehmen; sonst (Playlist ohne erzwungenes
        # Album) ein LEERES Album auf den Titel zurückfallen lassen, damit Navidrome
        # kein "[Unknown Album]" zeigt. Der Pipeline-Pfad (Album/Single) übergibt
        # IMMER album_name → der Fallback greift dort nie → Parität bit-identisch.
        current_album = get_tag_text(tags, "TALB")
        # Fallback nutzt den TATSÄCHLICH geschriebenen Titel (roh bei feat-off), damit
        # MP3 mit dem M4A/Opus-Pfad (_normalized_tags) übereinstimmt.
        effective_title = clean_title if options.feat_artist else title
        target_album = album_name or current_album or effective_title
        if target_album and current_album != target_album:
            print(f"  Album:       '{current_album}' → '{target_album}'")
            tags["TALB"] = TALB(encoding=3, text=target_album)
            changed = True

        # Cover-Bild ersetzen falls übergeben (nur wenn aktiviert)
        if options.cover and cover_data is not None:
            tags.delall("APIC")
            tags["APIC:"] = APIC(
                encoding=0,
                mime="image/jpeg",
                type=3,  # Front Cover
                desc="Cover",
                data=cover_data,
            )
            changed = True

        # Felder abgeschalteter Schalter entfernen (no-op wenn alle an → Parität)
        if _strip_id3(tags, options):
            changed = True

        if changed:
            tags.save(filepath, v2_version=3)  # ID3v2.3 für maximale Kompatibilität
            return True
        else:
            print(f"  [OK] {os.path.basename(filepath)}")
            return False

    except Exception as e:
        print(f"  [FEHLER] {os.path.basename(filepath)}: {e}")
        return False


def _normalized_tags(title: str, artist: str, albumartist: str, album_artist: str | None,
                     options: TagOptions = _ALL_ON):
    """Gemeinsame Logik: Titel/Artist/AlbumArtist nach Navidrome-Regeln.

    Liefert (clean_title, new_artist, correct_albumartist) oder None, wenn
    Titel/Artist fehlen (dann Datei überspringen — wie beim MP3-Pfad). Ist
    `options.feat_artist` aus, bleiben Titel/Artist roh (keine Bereinigung).
    """
    if not title or not artist:
        return None
    if options.feat_artist:
        clean_title, new_artist, _ = parse_featured_artists(title, artist)
    else:
        clean_title, new_artist = title, artist
    # Explicit album_artist wins (album/single always pass it → parity); otherwise
    # the primary is the first segment of the normalised artist — correct for
    # playlist per-track tagging (issue #11) and identical to the old raw split
    # when feat cleanup is off (new_artist == artist).
    correct_albumartist = album_artist if album_artist else new_artist.split(" / ")[0]
    return clean_title, new_artist, correct_albumartist


def process_file_mp4(filepath: str, cover_data: bytes | None = None, album_name: str | None = None, album_artist: str | None = None, options: TagOptions = _ALL_ON) -> bool:
    """Verarbeitet eine MP4/M4A-Datei (AAC/ALAC, iTunes-Atome)."""
    try:
        audio = MP4(filepath)

        def first(key):
            val = audio.tags.get(key) if audio.tags else None
            return str(val[0]).strip() if val else ""

        title, artist, albumartist = first("\xa9nam"), first("\xa9ART"), first("aART")
        norm = _normalized_tags(title, artist, albumartist, album_artist, options)
        if norm is None:
            print(f"  [SKIP] Kein Titel/Artist-Tag: {os.path.basename(filepath)}")
            return False
        clean_title, new_artist, correct_albumartist = norm

        changed = False
        if clean_title != title:
            print(f"  Titel:       '{title}' → '{clean_title}'")
            audio["\xa9nam"] = [clean_title]; changed = True
        if new_artist != artist:
            print(f"  Artist:      '{artist}' → '{new_artist}'")
            audio["\xa9ART"] = [new_artist]; changed = True
        if options.album_artist and albumartist != correct_albumartist:
            label = "(leer)" if not albumartist else f"'{albumartist}'"
            print(f"  AlbumArtist: {label} → '{correct_albumartist}'")
            audio["aART"] = [correct_albumartist]; changed = True
        cur_album = first("\xa9alb")
        target_album = album_name or cur_album or clean_title  # empty → title (issue #11)
        if target_album and cur_album != target_album:
            print(f"  Album:       '{cur_album}' → '{target_album}'")
            audio["\xa9alb"] = [target_album]; changed = True
        if options.cover and cover_data is not None:
            audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            changed = True
        if audio.tags is not None and _strip_dict(audio, options, _MP4_STRIP_KEYS):
            changed = True

        if changed:
            audio.save()
            return True
        print(f"  [OK] {os.path.basename(filepath)}")
        return False
    except Exception as e:
        print(f"  [FEHLER] {os.path.basename(filepath)}: {e}")
        return False


def _vorbis_cover(cover_data: bytes) -> str:
    """Base64-kodierter FLAC-Picture-Block (Front Cover) für Vorbis-Comments."""
    pic = Picture()
    pic.type = 3  # Front Cover
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = cover_data
    return base64.b64encode(pic.write()).decode("ascii")


def process_file_ogg(filepath: str, opener, cover_data: bytes | None = None, album_name: str | None = None, album_artist: str | None = None, options: TagOptions = _ALL_ON) -> bool:
    """Verarbeitet eine Opus-/Ogg-Datei (Vorbis-Comments, case-insensitive)."""
    try:
        audio = opener(filepath)

        def first(key):
            val = audio.get(key)
            return str(val[0]).strip() if val else ""

        title, artist, albumartist = first("title"), first("artist"), first("albumartist")
        norm = _normalized_tags(title, artist, albumartist, album_artist, options)
        if norm is None:
            print(f"  [SKIP] Kein Titel/Artist-Tag: {os.path.basename(filepath)}")
            return False
        clean_title, new_artist, correct_albumartist = norm

        changed = False
        if clean_title != title:
            print(f"  Titel:       '{title}' → '{clean_title}'")
            audio["title"] = [clean_title]; changed = True
        if new_artist != artist:
            print(f"  Artist:      '{artist}' → '{new_artist}'")
            audio["artist"] = [new_artist]; changed = True
        if options.album_artist and albumartist != correct_albumartist:
            label = "(leer)" if not albumartist else f"'{albumartist}'"
            print(f"  AlbumArtist: {label} → '{correct_albumartist}'")
            audio["albumartist"] = [correct_albumartist]; changed = True
        cur_album = first("album")
        target_album = album_name or cur_album or clean_title  # empty → title (issue #11)
        if target_album and cur_album != target_album:
            print(f"  Album:       '{cur_album}' → '{target_album}'")
            audio["album"] = [target_album]; changed = True
        if options.cover and cover_data is not None:
            audio["metadata_block_picture"] = [_vorbis_cover(cover_data)]
            audio.pop("coverart", None)  # älteres, nicht-standard Cover-Feld entfernen
            changed = True
        if _strip_dict(audio, options, _VORBIS_STRIP_KEYS):
            changed = True

        if changed:
            audio.save()
            return True
        print(f"  [OK] {os.path.basename(filepath)}")
        return False
    except Exception as e:
        print(f"  [FEHLER] {os.path.basename(filepath)}: {e}")
        return False


# Extension → Handler. MP3 bleibt der unveränderte ID3-Pfad.
_SUPPORTED_EXTS = (".mp3", ".m4a", ".mp4", ".opus", ".ogg", ".oga")


def _process_any(filepath: str, cover_data: bytes | None, album_name: str | None, album_artist: str | None, options: TagOptions = _ALL_ON) -> bool:
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".mp3":
        return process_file(filepath, cover_data, album_name, album_artist, options)
    if ext in (".m4a", ".mp4"):
        return process_file_mp4(filepath, cover_data, album_name, album_artist, options)
    if ext == ".opus":
        return process_file_ogg(filepath, OggOpus, cover_data, album_name, album_artist, options)
    return process_file_ogg(filepath, OggVorbis, cover_data, album_name, album_artist, options)


def process_directory(directory: str, cover_path: str | None = None, album_name: str | None = None, album_artist: str | None = None, options: TagOptions = _ALL_ON) -> None:
    """Verarbeitet alle unterstützten Audiodateien in einem Verzeichnis (nicht rekursiv)."""
    if not os.path.isdir(directory):
        print(f"Verzeichnis nicht gefunden: {directory}")
        sys.exit(1)

    mp3_files = sorted([
        f for f in os.listdir(directory)
        if f.lower().endswith(_SUPPORTED_EXTS)
    ])

    if not mp3_files:
        print(f"Keine Audiodateien in: {directory}")
        return

    # Cover-Datei einlesen
    cover_data = None
    if cover_path and os.path.isfile(cover_path):
        with open(cover_path, "rb") as f:
            cover_data = f.read()
        print(f"Cover: {cover_path} ({len(cover_data) // 1024} KB)")

    if album_name:
        print(f"Album:       '{album_name}' (wird auf alle Tracks gesetzt)")
    if album_artist:
        print(f"AlbumArtist: '{album_artist}' (wird auf alle Tracks erzwungen)")

    print(f"\nVerarbeite {len(mp3_files)} Dateien in: {directory}\n")

    changed_count = 0
    for filename in mp3_files:
        filepath = os.path.join(directory, filename)
        print(f"→ {filename}")
        if _process_any(filepath, cover_data, album_name, album_artist, options):
            changed_count += 1

    print(f"\n{changed_count} von {len(mp3_files)} Dateien aktualisiert.")


def process_tree(root: str, options: TagOptions = _ALL_ON, cover_for=None) -> list[str]:
    """Verarbeitet alle unterstützten Audiodateien unter `root` rekursiv (issue #11).

    Für Playlist-Downloads: die Tracks verteilen sich auf viele Interpreten/Alben,
    daher wird — anders als bei `process_directory` — KEIN gemeinsamer Album-Name /
    Album-Interpret erzwungen. Jeder Track wird aus seinen EIGENEN Metadaten
    normalisiert (Feat-Bereinigung + AlbumArtist = sein eigener Primärkünstler) über
    dieselben eingefrorenen Regeln (`_process_any`).

    `cover_for(filepath) -> bytes | None` liefert optional pro Track ein quadratisches
    Cover, das das (oft 16:9-)eingebettete Thumbnail ersetzt — sonst zeigt Navidrome
    unscharfe Ränder. Ohne Callback bleibt das eingebettete Thumbnail erhalten.

    Gibt die verarbeiteten Audiodateien in Bearbeitungsreihenfolge zurück (je Ordner
    nach Dateiname sortiert) — der Aufrufer nutzt sie z. B. für die m3u8-Tracklist,
    ohne denselben Ordner erneut scannen zu müssen.
    """
    if not os.path.isdir(root):
        print(f"Verzeichnis nicht gefunden: {root}")
        return []

    processed: list[str] = []
    changed = 0
    for dirpath, _dirs, files in os.walk(root):
        for filename in sorted(files):
            if not filename.lower().endswith(_SUPPORTED_EXTS):
                continue
            filepath = os.path.join(dirpath, filename)
            processed.append(filepath)
            cover = cover_for(filepath) if cover_for else None
            print(f"→ {os.path.relpath(filepath, root)}")
            if _process_any(filepath, cover, None, None, options):
                changed += 1

    print(f"\n{changed} von {len(processed)} Dateien aktualisiert.")
    return processed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Aufruf: fix_music_tags.py <Verzeichnis> [--cover cover.jpg]")
        print("Beispiel: fix_music_tags.py '/musik/Drake/CLB' --cover '/musik/Drake/CLB/cover.jpg'")
        sys.exit(1)

    directory = sys.argv[1]
    cover_path = None
    album_name = None

    # --cover Argument parsen
    if "--cover" in sys.argv:
        idx = sys.argv.index("--cover")
        if idx + 1 < len(sys.argv):
            cover_path = sys.argv[idx + 1]

    # --album Argument parsen
    if "--album" in sys.argv:
        idx = sys.argv.index("--album")
        if idx + 1 < len(sys.argv):
            album_name = sys.argv[idx + 1]

    # --artist Argument parsen (wird als AlbumArtist auf alle Tracks gesetzt)
    album_artist = None
    if "--artist" in sys.argv:
        idx = sys.argv.index("--artist")
        if idx + 1 < len(sys.argv):
            album_artist = sys.argv[idx + 1]

    process_directory(directory, cover_path, album_name, album_artist)
