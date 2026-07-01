"""Guards metadata parity: parse_options must turn the original flag lists into
the exact yt-dlp options the bash scripts produced."""
import shutil
import stat
import subprocess

import pytest
import yt_dlp

from app.fix_music_tags import TagOptions
from app.pipeline import (
    DEFAULT_AUDIO_FORMAT,
    _ALBUM_FLAGS,
    _PLAYLIST_TRACK_TMPL,
    _SINGLE_FLAGS,
    _apply_audio_format,
    _apply_cookie_policy,
    _apply_tag_options,
    _build_ydl_opts,
    _primary_artist,
    _safe_segment,
    _square_crop_jpeg,
    _write_cookie_file,
    _write_m3u,
    audio_format_short,
    is_supported_url,
    normalize_audio_format,
    pick_square_cover,
)


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker — avoids needing PIL/ffprobe."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None

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


def test_playlist_track_tmpl_orders_and_is_traversal_safe():
    # Playlist mode (issue #11): all tracks in one folder, filenames prefixed with a
    # zero-padded playlist index so they sort in playlist order; yt-dlp sanitises
    # %(title)s so a hostile title can't escape the folder. Assert via the real
    # download path (prepare_filename).
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "outtmpl": _PLAYLIST_TRACK_TMPL})
    assert ydl.prepare_filename(
        {"title": "Song A", "ext": "mp3", "playlist_index": 3}) == "0003 - Song A.mp3"
    # a track without an index (e.g. a single-video URL in playlist mode) still names cleanly
    assert ydl.prepare_filename({"title": "Solo", "ext": "mp3"}) == "NA - Solo.mp3"
    # path separators in the title are neutralised → stays a single filename, so a
    # hostile title cannot escape the playlist folder (the '..' dots are inert
    # without a real separator).
    out = ydl.prepare_filename({"title": "../../etc/x", "ext": "mp3", "playlist_index": 1})
    assert "/" not in out and "\\" not in out


def test_write_m3u_lists_tracks_in_order_relative(tmp_path):
    # The .m3u8 must reference tracks by bare filename (Navidrome resolves relative
    # to the file's folder) and preserve order (issue #11).
    (tmp_path / "0001 - A.mp3").write_bytes(b"")
    (tmp_path / "0002 - B.mp3").write_bytes(b"")
    tracks = sorted(tmp_path.glob("*.mp3"))
    m3u = _write_m3u(tmp_path, "My/Mix", tracks)

    assert m3u.name == "My_Mix.m3u8"                       # playlist name sanitised for the file
    body = m3u.read_text(encoding="utf-8").splitlines()
    assert body[0] == "#EXTM3U"
    # bare filenames, in order, no directory component
    track_lines = [ln for ln in body if not ln.startswith("#")]
    assert track_lines == ["0001 - A.mp3", "0002 - B.mp3"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_square_crop_jpeg_center_crops_to_square(tmp_path):
    # A 16:9 thumbnail must be cropped to a square so Navidrome shows no blurred bars
    # (issue #11). Generate a 640x360 image, crop, and assert the result is square.
    src = tmp_path / "wide.jpg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=640x360:rate=1", "-frames:v", "1", str(src)],
        check=True, capture_output=True,
    )
    out = _square_crop_jpeg(src)
    assert out and out[:2] == b"\xff\xd8"        # valid JPEG
    w, h = _jpeg_dimensions(out)
    assert w == h == 360                          # shorter side, centered


def test_write_m3u_neutralises_newline_injection(tmp_path):
    # A playlist/track title with an embedded newline must not forge extra m3u lines.
    (tmp_path / "0001 - A.mp3").write_bytes(b"")
    m3u = _write_m3u(tmp_path, "Evil\n#EXTINF:0,forged", sorted(tmp_path.glob("*.mp3")))
    body = m3u.read_text(encoding="utf-8").splitlines()
    assert body[1] == "#PLAYLIST:Evil #EXTINF:0,forged"      # newline collapsed to a space
    assert sum(1 for ln in body if ln.startswith("#PLAYLIST:")) == 1  # no forged directive line


def test_safe_segment_blocks_traversal():
    assert _safe_segment("AC/DC") == "AC_DC"
    assert _safe_segment("..") == "Unbekannt"
    assert _safe_segment("../../etc") == ".._.._etc"
    assert _safe_segment("  ") == "Unbekannt"
    assert _safe_segment("Drake") == "Drake"          # legitimate names untouched


def test_default_download_opts_use_no_cookies():
    # Without a user cookie, yt-dlp must use NO cookies — neither a server file nor
    # a browser store on the server (issue #9).
    for flags in (_ALBUM_FLAGS, _SINGLE_FLAGS):
        opts = _build_ydl_opts(flags + _OUT)
        assert opts.get("cookiefile") is None
        assert opts.get("cookiesfrombrowser") is None


def test_apply_cookie_policy_pins_user_cookie_and_forbids_browser():
    # No user cookie → cookiefile stays None, browser store forced off.
    opts = {}
    _apply_cookie_policy(opts, None)
    assert opts["cookiefile"] is None
    assert opts["cookiesfrombrowser"] is None

    # User cookie present → exactly that file is used, still no browser store.
    opts = {"cookiesfrombrowser": ("firefox",)}  # even if something set it, we override
    _apply_cookie_policy(opts, "/work/job.cookies.txt")
    assert opts["cookiefile"] == "/work/job.cookies.txt"
    assert opts["cookiesfrombrowser"] is None


def test_write_cookie_file_none_when_empty():
    # No cookie → no file → the download path stays byte-identical (issue #9 parity).
    assert _write_cookie_file("job-none", None) is None
    assert _write_cookie_file("job-empty", "") is None


def test_write_cookie_file_writes_verbatim_0600(tmp_path, monkeypatch):
    import app.pipeline as pipeline

    monkeypatch.setattr(pipeline, "_WORK_ROOT", tmp_path / ".work")
    content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tabc\n"

    path = pipeline._write_cookie_file("job-42", content)

    assert path is not None
    assert path.read_text() == content                       # verbatim, no mangling
    assert stat.S_IMODE(path.stat().st_mode) == 0o600        # owner-only
    # kept OUTSIDE the per-job work dir so the WebDAV upload never ships it
    assert path.name == "job-42.cookies.txt"
    assert (pipeline._WORK_ROOT / "job-42") not in path.parents
