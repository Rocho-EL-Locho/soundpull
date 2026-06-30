#!/usr/bin/env python3
"""
fix_music_tags.py — Korrigiert Featured Artist Metadaten in MP3-Dateien

Navidrome-konforme Regeln (ID3v2.3 / MP3):
  - ARTIST:      "Primärkünstler / Featured Artist"  (Trennzeichen: " / ")
  - ALBUMARTIST: Nur Primärkünstler (unverändert)
  - TITLE:       Ohne "(feat. ...)" Anteil

Aufruf: python3 fix_music_tags.py <Verzeichnis>
"""

import os
import re
import sys
from mutagen.id3 import ID3, TPE1, TPE2, TIT2, TALB, APIC, ID3NoHeaderError


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


def process_directory(directory: str, cover_path: str | None = None, album_name: str | None = None, album_artist: str | None = None) -> None:
    """Verarbeitet alle MP3-Dateien in einem Verzeichnis (nicht rekursiv)."""
    if not os.path.isdir(directory):
        print(f"Verzeichnis nicht gefunden: {directory}")
        sys.exit(1)

    mp3_files = sorted([
        f for f in os.listdir(directory)
        if f.lower().endswith(".mp3")
    ])

    if not mp3_files:
        print(f"Keine MP3-Dateien in: {directory}")
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
        if process_file(filepath, cover_data, album_name, album_artist):
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
