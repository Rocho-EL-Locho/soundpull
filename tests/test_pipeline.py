"""Guards metadata parity: parse_options must turn the original flag lists into
the exact yt-dlp options the bash scripts produced."""
from app.fix_music_tags import TagOptions
from app.pipeline import (
    DEFAULT_AUDIO_FORMAT,
    _ALBUM_FLAGS,
    _SINGLE_FLAGS,
    _apply_audio_format,
    _apply_tag_options,
    _build_ydl_opts,
    _primary_artist,
    _safe_segment,
    audio_format_short,
    is_supported_url,
    normalize_audio_format,
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


def test_default_audio_format_is_noop_parity():
    # The default (mp3_320) must not alter the original flag lists at all —
    # byte-identical flags → byte-identical tags (the parity invariant).
    assert _apply_audio_format(_ALBUM_FLAGS, DEFAULT_AUDIO_FORMAT) == _ALBUM_FLAGS
    assert _apply_audio_format(_SINGLE_FLAGS, DEFAULT_AUDIO_FORMAT) == _SINGLE_FLAGS


def test_mp3_192_changes_only_the_bitrate():
    opts = _build_ydl_opts(_apply_audio_format(_ALBUM_FLAGS, "mp3_192") + _OUT)
    extract = next(pp for pp in opts["postprocessors"] if pp["key"] == "FFmpegExtractAudio")
    assert extract["preferredcodec"] == "mp3"
    assert extract["preferredquality"] == "192"


def test_original_drops_format_and_quality_so_source_is_remuxed():
    flags = _apply_audio_format(_ALBUM_FLAGS, "original")
    assert "--audio-format" not in flags    # no codec target → keep source (no re-encode)
    assert "--audio-quality" not in flags
    extract = next(pp for pp in _build_ydl_opts(flags + _OUT)["postprocessors"]
                   if pp["key"] == "FFmpegExtractAudio")
    assert extract["preferredcodec"] == "best"   # 'best' = copy the source stream


def test_tag_options_all_on_is_noop_parity():
    # All fields on (default) must not alter the flag lists → byte-identical tags.
    assert _apply_tag_options(_ALBUM_FLAGS, TagOptions()) == _ALBUM_FLAGS
    assert _apply_tag_options(_SINGLE_FLAGS, TagOptions()) == _SINGLE_FLAGS


def test_cover_off_drops_thumbnail_flags():
    flags = _apply_tag_options(_ALBUM_FLAGS, TagOptions(cover=False))
    assert "--embed-thumbnail" not in flags
    assert "--convert-thumbnails" not in flags
    assert "jpg" not in flags                       # the orphaned value is removed too
    assert "EmbedThumbnail" not in _pp_keys(_build_ydl_opts(flags + _OUT))


def test_track_off_drops_playlist_remap_album_only():
    album = _apply_tag_options(_ALBUM_FLAGS, TagOptions(track_number=False))
    assert "--parse-metadata" not in album
    assert "playlist_index:%(track_number)s" not in album
    assert "MetadataParser" not in _pp_keys(_build_ydl_opts(album + _OUT))
    # singles never had the playlist→track remap, so the flag list is untouched.
    assert _apply_tag_options(_SINGLE_FLAGS, TagOptions(track_number=False)) == _SINGLE_FLAGS


def test_normalize_audio_format_clamps_unknown_to_default():
    assert normalize_audio_format("bogus") == DEFAULT_AUDIO_FORMAT
    assert normalize_audio_format(None) == DEFAULT_AUDIO_FORMAT
    assert normalize_audio_format("mp3_128") == DEFAULT_AUDIO_FORMAT   # retired tier
    assert normalize_audio_format("original") == "original"


def test_audio_format_short_labels():
    assert audio_format_short("mp3_320") == "MP3 320"
    assert audio_format_short("mp3_192") == "MP3 192"
    assert audio_format_short("original") == "Original"


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


def test_is_supported_url_accepts_youtube_hosts():
    assert is_supported_url("https://music.youtube.com/watch?v=abc")
    assert is_supported_url("https://www.youtube.com/playlist?list=x")
    assert is_supported_url("https://youtu.be/abc")


def test_is_supported_url_rejects_lookalikes_and_non_http():
    assert not is_supported_url("https://youtube.com.evil.com/x")  # not a youtube host
    assert not is_supported_url("https://evil.com/youtube.com")     # substring only
    assert not is_supported_url("file:///etc/passwd")               # wrong scheme
    assert not is_supported_url("")


def test_safe_segment_blocks_traversal():
    assert _safe_segment("AC/DC") == "AC_DC"
    assert _safe_segment("..") == "Unbekannt"
    assert _safe_segment("../../etc") == ".._.._etc"
    assert _safe_segment("  ") == "Unbekannt"
    assert _safe_segment("Drake") == "Drake"          # legitimate names untouched
