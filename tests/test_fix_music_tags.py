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


# A feat./ft./featuring marker inside the ARTIST tag (not the title) is normalised to
# " / " too — deliberate deviation from the frozen original, which left it verbatim.
def test_feat_in_artist_tag_is_normalized():
    assert parse_featured_artists("Song", "A feat. B") == ("Song", "A / B", "A")
    assert parse_featured_artists("Song", "A ft. B") == ("Song", "A / B", "A")
    assert parse_featured_artists("Song", "A featuring B") == ("Song", "A / B", "A")
    # '&'/','/'und' inside the feature part are split, so "A ft. B & C" → "A / B / C".
    assert parse_featured_artists("Song", "A ft. B & C") == ("Song", "A / B / C", "A")


def test_feat_in_both_title_and_artist_tag_dedupes():
    # Was buggy: the primary retained the "feat. B" marker → "A feat. B / B". Now clean.
    assert parse_featured_artists("Song (feat. B)", "A feat. B") == ("Song", "A / B", "A")


def test_collab_separators_in_artist_tag_are_kept():
    # '&'/' x '/' und ' are NOT split — they also occur in real band names.
    assert parse_featured_artists("Song", "A & B") == ("Song", "A & B", "A & B")
    assert parse_featured_artists("Song", "A x B") == ("Song", "A x B", "A x B")
    assert parse_featured_artists("Song", "A und B") == ("Song", "A und B", "A und B")
    # A band name with '&' in the PRIMARY part survives a feat. on the same tag.
    assert parse_featured_artists("Song", "Simon & Garfunkel feat. B") \
        == ("Song", "Simon & Garfunkel / B", "Simon & Garfunkel")


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


def test_albumartist_fallback_uses_normalized_primary_when_none_given(tmp_path):
    # No explicit album_artist (playlist per-track tagging / CLI): the album artist
    # is the first segment of the NORMALISED artist, so a comma list "A, B" yields
    # "A" — not the whole raw tag (issue #11).
    p = str(tmp_path / "a.mp3")
    _make_mp3(p)  # TPE1 "A, B", title "Song (feat. B)"
    fmt.process_file(p, options=TagOptions())  # album_artist omitted → fallback
    assert str(ID3(p)["TPE2"]) == "A"


def test_album_falls_back_to_title_when_empty_and_none_given(tmp_path):
    # Playlist per-track tagging (issue #11): a track with NO album tag gets its
    # (feat-stripped) title as the album, so Navidrome shows no "[Unknown Album]".
    p = str(tmp_path / "a.mp3")
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Some Song (feat. B)"))
    tags.add(TPE1(encoding=3, text="A, B"))
    tags.save(p, v2_version=3)  # deliberately NO TALB
    fmt.process_file(p, options=TagOptions())  # album_name omitted → fallback
    assert str(ID3(p)["TALB"]) == "Some Song"


def test_album_fallback_uses_raw_title_when_feat_off(tmp_path):
    # Consistency with the M4A/Opus path: with feat cleanup off, the empty-album
    # fallback uses the RAW title (as actually written), not the feat-stripped one.
    p = str(tmp_path / "a.mp3")
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Song (feat. B)"))
    tags.add(TPE1(encoding=3, text="A, B"))
    tags.save(p, v2_version=3)  # no TALB
    fmt.process_file(p, options=TagOptions(feat_artist=False))
    assert str(ID3(p)["TALB"]) == "Song (feat. B)"   # raw, matches the left-raw title tag
    assert str(ID3(p)["TIT2"]) == "Song (feat. B)"


def test_album_kept_when_present_and_none_given(tmp_path):
    # A track that already has an album keeps it — the title fallback only fills an
    # EMPTY album, it never overwrites a real one.
    p = str(tmp_path / "a.mp3")
    _make_mp3(p)  # TALB "X"
    fmt.process_file(p, options=TagOptions())  # album_name omitted
    assert str(ID3(p)["TALB"]) == "X"


def test_albumartist_fallback_feat_off_matches_raw_across_formats(tmp_path):
    # With feat cleanup OFF and no explicit album_artist, the album artist is the
    # first segment of the RAW artist — the MP3 path and the shared M4A/Opus path
    # (_normalized_tags) must agree, no format-dependent divergence (issue #11).
    p = str(tmp_path / "a.mp3")
    _make_mp3(p)  # TPE1 "A, B"
    fmt.process_file(p, options=TagOptions(feat_artist=False))
    assert str(ID3(p)["TPE2"]) == "A, B"  # raw first segment (no " / "), not parsed "A"
    assert _normalized_tags("Song", "A, B", "", None, TagOptions(feat_artist=False))[2] == "A, B"


def test_process_tree_embeds_per_track_cover_via_callback(tmp_path):
    # Playlist tracks get a per-track square cover embedded (issue #11): process_tree
    # calls cover_for(filepath) and writes the returned bytes as the APIC, replacing
    # the (often 16:9) embedded thumbnail that would otherwise show blurred bars.
    p = tmp_path / "0001 - A.mp3"
    _make_mp3(str(p))  # starts with APIC data b"OLD"
    fmt.process_tree(str(tmp_path), TagOptions(), cover_for=lambda fp: b"SQUARE")
    assert ID3(str(p)).getall("APIC")[0].data == b"SQUARE"


def test_process_tree_keeps_embedded_thumbnail_when_no_cover_supplied(tmp_path):
    # No cover_for (or it returns None) → the embedded thumbnail is left untouched.
    p = tmp_path / "0001 - A.mp3"
    _make_mp3(str(p))
    fmt.process_tree(str(tmp_path), TagOptions())
    assert ID3(str(p)).getall("APIC")[0].data == b"OLD"


def test_process_tree_tags_each_track_from_its_own_metadata(tmp_path):
    # A playlist (issue #11) spans many artists/albums: process_tree must walk the
    # whole tree and normalise each track from its OWN tags — no shared album name
    # or album-artist forced across tracks (unlike process_directory).
    d1 = tmp_path / "A" / "AlbumX"
    d2 = tmp_path / "C" / "AlbumY"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    _make_mp3(str(d1 / "t1.mp3"))  # title "Song (feat. B)", artist "A, B", album "X"

    tags = ID3()
    tags.add(TIT2(encoding=3, text="Other (feat. Z)"))
    tags.add(TPE1(encoding=3, text="C, Z"))
    tags.add(TALB(encoding=3, text="AlbumY"))
    tags.save(str(d2 / "t2.mp3"), v2_version=3)

    fmt.process_tree(str(tmp_path), TagOptions())

    t1 = ID3(str(d1 / "t1.mp3"))
    assert str(t1["TIT2"]) == "Song"          # own feat cleanup applied
    assert str(t1["TPE1"]) == "A / B"
    assert str(t1["TPE2"]) == "A"             # album-artist = its own primary artist
    assert str(t1["TALB"]) == "X"             # own album kept, NOT unified

    t2 = ID3(str(d2 / "t2.mp3"))
    assert str(t2["TIT2"]) == "Other"
    assert str(t2["TPE1"]) == "C / Z"
    assert str(t2["TPE2"]) == "C"
    assert str(t2["TALB"]) == "AlbumY"        # each track keeps its own album


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
