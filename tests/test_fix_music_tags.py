"""Guards the Navidrome tag rules (feat-artist handling) — the crown jewel.

Also covers the per-field write/strip gating (issue #7): the default (all on) keeps
the original behaviour, and each toggled-off field is removed across MP3/M4A/Opus.
"""
import shutil
import subprocess

import pytest
from mutagen.id3 import ID3, APIC, TALB, TCON, TIT2, TPE1, TPE2, TRCK, TXXX
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus

import app.fix_music_tags as fmt
from app.fix_music_tags import (
    TagOptions,
    _normalized_tags,
    parse_featured_artists,
    split_artists,
)


def test_split_artists_separators():
    assert split_artists("A & B, C") == ["A", "B", "C"]
    assert split_artists("A und B") == ["A", "B"]


def test_feat_in_title_moves_to_artist_and_cleans_title():
    title, artist, album_artist = parse_featured_artists("Song (feat. B)", "A, B")
    assert title == "Song"
    assert artist == "A / B"          # Primary / Featured
    assert album_artist == "A"        # album artist = primary only


def test_comma_list_without_feat_is_normalized():
    title, artist, album_artist = parse_featured_artists("Song", "A, B")
    assert artist == "A / B"
    assert album_artist == "A"


def test_plain_single_artist_unchanged():
    assert parse_featured_artists("Song", "A") == ("Song", "A", "A")


# _normalized_tags is the shared path the M4A and Opus/OGG adapters route through,
# so the "original codec" download gets the exact same feat/album-artist rules.
def test_normalized_tags_applies_feat_rules_with_explicit_album_artist():
    # In the pipeline the primary artist is always passed as album_artist → it wins.
    assert _normalized_tags("Song (feat. B)", "A, B", "", "A") == ("Song", "A / B", "A")


def test_normalized_tags_skips_when_title_or_artist_missing():
    assert _normalized_tags("", "A", "", "A") is None
    assert _normalized_tags("Song", "", "", "A") is None


def test_normalized_tags_feat_off_keeps_raw_title_and_artist():
    # feat_artist off → no cleanup; title/artist stay exactly as yt-dlp wrote them.
    assert _normalized_tags("Song (feat. B)", "A, B", "", "A", TagOptions(feat_artist=False)) \
        == ("Song (feat. B)", "A, B", "A")


# --- File round-trips for the per-field gating (issue #7) ----------------------
# MP3 uses only mutagen (no audio needed); M4A/Opus need a real container, built
# with ffmpeg (a hard runtime dep) and skipped if it's absent.

def _make_mp3(path: str) -> None:
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Song (feat. B)"))
    tags.add(TPE1(encoding=3, text="A, B"))
    tags.add(TPE2(encoding=3, text="OLD"))
    tags.add(TALB(encoding=3, text="X"))
    tags.add(TCON(encoding=3, text="Rap"))
    tags.add(TRCK(encoding=3, text="3"))
    tags.add(TXXX(encoding=3, desc="comment", text="https://youtu.be/x"))
    tags.add(TXXX(encoding=3, desc="description", text="long"))
    tags.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="Cover", data=b"OLD"))
    tags.save(path, v2_version=3)


def test_mp3_all_on_applies_navidrome_and_keeps_every_field(tmp_path):
    p = str(tmp_path / "a.mp3")
    _make_mp3(p)
    fmt.process_file(p, cover_data=b"NEW", album_name="X", album_artist="A", options=TagOptions())
    tags = ID3(p)
    assert str(tags["TIT2"]) == "Song"          # (feat. B) stripped
    assert str(tags["TPE1"]) == "A / B"          # Primary / Feat
    assert str(tags["TPE2"]) == "A"              # album artist forced to primary
    assert tags.getall("APIC")[0].data == b"NEW"  # square cover embedded
    for kept in ("TCON", "TRCK", "TXXX:comment", "TXXX:description"):
        assert kept in tags, f"{kept} must survive when all toggles on"


@pytest.mark.parametrize("opt,gone", [
    ({"genre": False}, "TCON"),
    ({"track_number": False}, "TRCK"),
    ({"album_artist": False}, "TPE2"),
    ({"cover": False}, "APIC"),
])
def test_mp3_toggle_off_strips_its_frame(tmp_path, opt, gone):
    p = str(tmp_path / "a.mp3")
    _make_mp3(p)
    fmt.process_file(p, cover_data=b"NEW", album_name="X", album_artist="A", options=TagOptions(**opt))
    assert not ID3(p).getall(gone)


def test_mp3_comments_off_strips_comment_like_txxx(tmp_path):
    p = str(tmp_path / "a.mp3")
    _make_mp3(p)
    fmt.process_file(p, album_artist="A", options=TagOptions(comments=False))
    tags = ID3(p)
    assert "TXXX:comment" not in tags
    assert "TXXX:description" not in tags


def test_mp3_feat_off_keeps_raw_title_and_artist(tmp_path):
    p = str(tmp_path / "a.mp3")
    _make_mp3(p)
    fmt.process_file(p, album_artist="A", options=TagOptions(feat_artist=False))
    tags = ID3(p)
    assert str(tags["TIT2"]) == "Song (feat. B)"
    assert str(tags["TPE1"]) == "A, B"


_FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not on PATH")


def _ffmpeg_make(path: str, codec: str) -> None:
    subprocess.run(
        [_FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.2",
         "-metadata", "title=Song (feat. B)", "-metadata", "artist=A, B",
         "-metadata", "album_artist=OLD", "-metadata", "album=X",
         "-metadata", "genre=Rap", "-metadata", "track=3",
         "-metadata", "comment=https://youtu.be/x", "-metadata", "description=long",
         "-codec:a", codec, path],
        check=True, capture_output=True,
    )


@needs_ffmpeg
def test_m4a_all_on_keeps_fields_and_off_strips_them(tmp_path):
    p = str(tmp_path / "a.m4a")
    _ffmpeg_make(p, "aac")
    fmt.process_file_mp4(p, album_artist="A", options=TagOptions())
    tags = MP4(p).tags
    assert tags["\xa9nam"][0] == "Song" and tags["aART"][0] == "A"
    assert "\xa9gen" in tags and "trkn" in tags and "\xa9cmt" in tags

    _ffmpeg_make(p, "aac")
    fmt.process_file_mp4(p, album_artist="A",
                         options=TagOptions(genre=False, track_number=False, comments=False))
    tags = MP4(p).tags
    assert "\xa9gen" not in tags and "trkn" not in tags
    assert "\xa9cmt" not in tags and "desc" not in tags


@needs_ffmpeg
def test_opus_all_on_keeps_fields_and_off_strips_them(tmp_path):
    p = str(tmp_path / "a.opus")
    _ffmpeg_make(p, "libopus")
    fmt.process_file_ogg(p, OggOpus, album_artist="A", options=TagOptions())
    audio = OggOpus(p)
    assert audio["title"][0] == "Song" and audio["albumartist"][0] == "A"
    assert "genre" in audio and "tracknumber" in audio

    _ffmpeg_make(p, "libopus")
    fmt.process_file_ogg(p, OggOpus, album_artist="A",
                         options=TagOptions(genre=False, track_number=False, comments=False))
    audio = OggOpus(p)
    assert "genre" not in audio and "tracknumber" not in audio
    assert "comment" not in audio and "description" not in audio
