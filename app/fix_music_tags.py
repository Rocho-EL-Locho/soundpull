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
from mutagen.flac import Picture
from mutagen.id3 import ID3, TPE1, TPE2, TIT2, TALB, APIC, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis


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

    if not featured_from_title:
        # Kein feat. im Titel → nur Komma-Trennung normalisieren falls vorhanden
        if ", " in artist:
            parts = [a.strip() for a in artist.split(", ")]
            return title, " / ".join(parts), parts[0]
        return title, artist, artist

    # yt-dlp schreibt Artist-Tag oft als "Primär, Feat1, Feat2" (komma-separiert).
    # Wir extrahieren den Primärkünstler als den Teil VOR dem ersten ", Feat-Artist".
    # Strategie: Entferne alle bekannten Featured Artists aus dem Artist-Tag
    # um den Primärkünstler zu isolieren.
    artist_parts = [a.strip() for a in artist.split(", ")]

    # Normalisiere Featured Artists (lowercase für Vergleich)
    feat_lower = {f.lower() for f in featured_from_title}

    # Primärkünstler = alle Teile die NICHT in den Featured Artists sind
    primary_parts = [p for p in artist_parts if p.lower() not in feat_lower]
    primary_artist = ", ".join(primary_parts) if primary_parts else artist_parts[0]

    # Finaler Artist-Tag: Primärkünstler / Feat1 / Feat2
    all_artists = [primary_artist] + featured_from_title
    new_artist = " / ".join(all_artists)

    return clean_title, new_artist, primary_artist


def get_tag_text(tags, key: str) -> str:
    """Liest einen ID3-Tag sicher als String."""
    frame = tags.get(key)
    if frame is None:
        return ""
    return str(frame).strip()


def process_file(filepath: str, cover_data: bytes | None = None, album_name: str | None = None, album_artist: str | None = None) -> bool:
    """
    Verarbeitet eine einzelne MP3-Datei.
    cover_data: optionale JPG-Bytes für das Cover-Bild (ersetzt das eingebettete Thumbnail)
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

        clean_title, new_artist, new_albumartist = parse_featured_artists(title, artist)

        changed = False

        if clean_title != title:
            print(f"  Titel:       '{title}' → '{clean_title}'")
            tags["TIT2"] = TIT2(encoding=3, text=clean_title)
            changed = True

        if new_artist != artist:
            print(f"  Artist:      '{artist}' → '{new_artist}'")
            tags["TPE1"] = TPE1(encoding=3, text=new_artist)
            changed = True

        # AlbumArtist: immer auf den korrekten Primärkünstler setzen.
        # Priorität: explizit übergebener album_artist > erster Teil des Artist-Tags
        correct_albumartist = album_artist if album_artist else artist.split(" / ")[0]
        if albumartist != correct_albumartist:
            label = "(leer)" if not albumartist else f"'{albumartist}'"
            print(f"  AlbumArtist: {label} → '{correct_albumartist}'")
            tags["TPE2"] = TPE2(encoding=3, text=correct_albumartist)
            changed = True

        # Album-Name vereinheitlichen falls übergeben
        if album_name:
            current_album = get_tag_text(tags, "TALB")
            if current_album != album_name:
                print(f"  Album:       '{current_album}' → '{album_name}'")
                tags["TALB"] = TALB(encoding=3, text=album_name)
                changed = True

        # Cover-Bild ersetzen falls übergeben
        if cover_data is not None:
            tags.delall("APIC")
            tags["APIC:"] = APIC(
                encoding=0,
                mime="image/jpeg",
                type=3,  # Front Cover
                desc="Cover",
                data=cover_data,
            )
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


def _normalized_tags(title: str, artist: str, albumartist: str, album_artist: str | None):
    """Gemeinsame Logik: Titel/Artist/AlbumArtist nach Navidrome-Regeln.

    Liefert (clean_title, new_artist, correct_albumartist) oder None, wenn
    Titel/Artist fehlen (dann Datei überspringen — wie beim MP3-Pfad).
    """
    if not title or not artist:
        return None
    clean_title, new_artist, _ = parse_featured_artists(title, artist)
    correct_albumartist = album_artist if album_artist else artist.split(" / ")[0]
    return clean_title, new_artist, correct_albumartist


def process_file_mp4(filepath: str, cover_data: bytes | None = None, album_name: str | None = None, album_artist: str | None = None) -> bool:
    """Verarbeitet eine MP4/M4A-Datei (AAC/ALAC, iTunes-Atome)."""
    try:
        audio = MP4(filepath)

        def first(key):
            val = audio.tags.get(key) if audio.tags else None
            return str(val[0]).strip() if val else ""

        title, artist, albumartist = first("\xa9nam"), first("\xa9ART"), first("aART")
        norm = _normalized_tags(title, artist, albumartist, album_artist)
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
        if albumartist != correct_albumartist:
            label = "(leer)" if not albumartist else f"'{albumartist}'"
            print(f"  AlbumArtist: {label} → '{correct_albumartist}'")
            audio["aART"] = [correct_albumartist]; changed = True
        cur_album = first("\xa9alb")
        if album_name and cur_album != album_name:
            print(f"  Album:       '{cur_album}' → '{album_name}'")
            audio["\xa9alb"] = [album_name]; changed = True
        if cover_data is not None:
            audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
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


def process_file_ogg(filepath: str, opener, cover_data: bytes | None = None, album_name: str | None = None, album_artist: str | None = None) -> bool:
    """Verarbeitet eine Opus-/Ogg-Datei (Vorbis-Comments, case-insensitive)."""
    try:
        audio = opener(filepath)

        def first(key):
            val = audio.get(key)
            return str(val[0]).strip() if val else ""

        title, artist, albumartist = first("title"), first("artist"), first("albumartist")
        norm = _normalized_tags(title, artist, albumartist, album_artist)
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
        if albumartist != correct_albumartist:
            label = "(leer)" if not albumartist else f"'{albumartist}'"
            print(f"  AlbumArtist: {label} → '{correct_albumartist}'")
            audio["albumartist"] = [correct_albumartist]; changed = True
        cur_album = first("album")
        if album_name and cur_album != album_name:
            print(f"  Album:       '{cur_album}' → '{album_name}'")
            audio["album"] = [album_name]; changed = True
        if cover_data is not None:
            audio["metadata_block_picture"] = [_vorbis_cover(cover_data)]
            audio.pop("coverart", None)  # älteres, nicht-standard Cover-Feld entfernen
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


def _process_any(filepath: str, cover_data: bytes | None, album_name: str | None, album_artist: str | None) -> bool:
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".mp3":
        return process_file(filepath, cover_data, album_name, album_artist)
    if ext in (".m4a", ".mp4"):
        return process_file_mp4(filepath, cover_data, album_name, album_artist)
    if ext == ".opus":
        return process_file_ogg(filepath, OggOpus, cover_data, album_name, album_artist)
    return process_file_ogg(filepath, OggVorbis, cover_data, album_name, album_artist)


def process_directory(directory: str, cover_path: str | None = None, album_name: str | None = None, album_artist: str | None = None) -> None:
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
        if _process_any(filepath, cover_data, album_name, album_artist):
            changed_count += 1

    print(f"\n{changed_count} von {len(mp3_files)} Dateien aktualisiert.")


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
