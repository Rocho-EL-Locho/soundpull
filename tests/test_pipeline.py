"""Guards metadata parity: parse_options must turn the original flag lists into
the exact yt-dlp options the bash scripts produced."""
from app.pipeline import (
    _ALBUM_FLAGS,
    _SINGLE_FLAGS,
    _build_ydl_opts,
    _primary_artist,
    pick_square_cover,
)

_OUT = ["-o", "/tmp/x/%(title)s.%(ext)s"]


def _pp_keys(opts):
    return [pp["key"] for pp in opts.get("postprocessors", [])]


def test_album_opts_parity():
    opts = _build_ydl_opts(_ALBUM_FLAGS + _OUT)
    assert opts["format"] == "bestaudio/best"
    keys = _pp_keys(opts)
    for expected in ("FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail", "MetadataParser"):
        assert expected in keys, f"missing {expected}"
    extract = next(pp for pp in opts["postprocessors"] if pp["key"] == "FFmpegExtractAudio")
    assert extract["preferredcodec"] == "mp3"
    assert extract["preferredquality"] == "320"
    clients = opts["extractor_args"]["youtube"]["player_client"]
    assert {"ios", "web", "android"}.issubset(set(clients))


def test_single_has_no_playlist_track_remap():
    opts = _build_ydl_opts(_SINGLE_FLAGS + _OUT)
    keys = _pp_keys(opts)
    assert "MetadataParser" not in keys           # singles keep their own track no.
    assert "FFmpegExtractAudio" in keys           # but still mp3 extraction
    assert "EmbedThumbnail" in keys


def test_primary_artist_extraction():
    assert _primary_artist("A, B, C") == "A"
    assert _primary_artist("Drake") == "Drake"
    assert _primary_artist(None) == "Unbekannt"
    assert _primary_artist("NA") == "Unbekannt"


def test_pick_square_cover_prefers_signed_then_largest():
    thumbs = [
        {"url": "u/a", "width": 100, "height": 100},
        {"url": "u/b?sqp=x", "width": 300, "height": 300},
        {"url": "u/c", "width": 500, "height": 500},
        {"url": "u/wide", "width": 800, "height": 450},
    ]
    assert pick_square_cover(thumbs) == "u/b?sqp=x"   # signed wins over larger unsigned
    assert pick_square_cover([]) is None
