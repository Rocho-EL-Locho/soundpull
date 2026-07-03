"""Guards metadata parity: parse_options must turn the original flag lists into
the exact yt-dlp options the bash scripts produced."""
import posixpath
import shutil
import stat
import subprocess

import pytest
import yt_dlp

from app.fix_music_tags import TagOptions
from app.pipeline import (
    DEFAULT_AUDIO_FORMAT,
    Destination,
    Reporter,
    Result,
    _ALBUM_FLAGS,
    _PLAYLIST_TRACK_TMPL,
    _SINGLE_FLAGS,
    enumerate_artist,
    run_artist_download,
    _apply_audio_format,
    _apply_cookie_policy,
    _apply_tag_options,
    _build_playlist_manifest,
    _build_ydl_opts,
    _credits_artist,
    _genre_flags,
    _index_from_name,
    _make_match_filter,
    _merge_manifest,
    _playlist_folder_name,
    _primary_artist,
    _safe_segment,
    _square_crop_jpeg,
    _write_cookie_file,
    _write_m3u,
    _write_m3u_entries,
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


# Frozen postprocessor chains for the pinned yt-dlp (== in pyproject, issue #37).
# The full ORDERED key list — not just presence — so a yt-dlp bump that reorders,
# drops, adds or reworks a postprocessor fails CI instead of silently shipping a
# changed extraction/tagging path. Re-verify against real tag output, then update
# these lists deliberately, when bumping the pin.
_ALBUM_PP_CHAIN = [
    "MetadataParser",
    "FFmpegThumbnailsConvertor",
    "FFmpegExtractAudio",
    "FFmpegMetadata",
    "EmbedThumbnail",
    "FFmpegConcat",
]
_SINGLE_PP_CHAIN = [
    "FFmpegThumbnailsConvertor",
    "FFmpegExtractAudio",
    "FFmpegMetadata",
    "EmbedThumbnail",
    "FFmpegConcat",
]


def test_postprocessor_chain_snapshot_parity():
    # Snapshot guard for the metadata-parity invariant: the pinned yt-dlp must
    # produce exactly this postprocessor chain (mp3 / 320 extraction included). A
    # bump that changes the chain must break this test — that's the whole point.
    for flags, expected in ((_ALBUM_FLAGS, _ALBUM_PP_CHAIN), (_SINGLE_FLAGS, _SINGLE_PP_CHAIN)):
        opts = _build_ydl_opts(flags + _OUT)
        assert _pp_keys(opts) == expected
        extract = next(pp for pp in opts["postprocessors"] if pp["key"] == "FFmpegExtractAudio")
        assert extract["preferredcodec"] == "mp3"
        assert extract["preferredquality"] == "320"


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


def test_playlist_folder_name_disambiguates_by_id():
    # Two different playlists that share a title must NOT map to the same folder,
    # or the second delivery overwrites the first's .m3u8 / clobbers tracks (issue #39).
    a = _playlist_folder_name("Chill", "PLaaaa")
    b = _playlist_folder_name("Chill", "PLbbbb")
    assert a != b
    assert a == "Chill [PLaaaa]"
    # The id is stable per URL, so a re-sync of the SAME playlist lands in the SAME folder.
    assert _playlist_folder_name("Chill", "PLaaaa") == a


def test_playlist_folder_name_without_id_falls_back_to_title():
    # No id exposed by the extractor → keep the bare (sanitised) title.
    assert _playlist_folder_name("Chill", "") == "Chill"


def test_playlist_folder_name_is_traversal_safe():
    # The disambiguated name is still one safe path segment (no escaping the work dir).
    assert _playlist_folder_name("../../etc", "PLx") == ".._.._etc [PLx]"
    assert "/" not in _playlist_folder_name("A/B", "PLx")


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


def test_genre_flags_forces_real_genre_but_skips_empty():
    # A real genre forces the metadata override exactly as before (parity)…
    assert _genre_flags("Rap") == ["--postprocessor-args", "ffmpeg:-metadata genre=Rap"]
    # …while "no genre" (empty/blank) skips it → track keeps its own genre (issue #21).
    assert _genre_flags("") == []
    assert _genre_flags("   ") == []
    assert _genre_flags(None) == []


def test_default_download_opts_set_no_match_filter():
    # Parity (issue #21): a normal (non-sync) download must not carry a match_filter
    # that could skip tracks — parse_options leaves it None.
    for flags in (_ALBUM_FLAGS, _SINGLE_FLAGS):
        assert _build_ydl_opts(flags + _OUT).get("match_filter") is None


def test_match_filter_rejects_tracks_on_server():
    # The sync match-filter skips a track already on the server, keeps a new one,
    # defers while incomplete, and ignores the playlist envelope (issue #21).
    known = {("drake", "hotline bling")}
    mf = _make_match_filter(lambda artist, title: (
        # mirror library_index.track_key just enough for the test's inputs
        artist.split(",")[0].strip().casefold(),
        title.casefold(),
    ) in known)

    # already on server → rejected (non-None reason string)
    assert mf({"_type": "video", "title": "hotline bling", "artist": "drake"}) is not None
    # new track → downloaded (None)
    assert mf({"_type": "video", "title": "gods plan", "artist": "drake"}) is None
    # partial metadata during enumeration → defer (never reject early)
    assert mf({"title": "hotline bling", "artist": "drake"}, incomplete=True) is None
    # the playlist container itself is never filtered out
    assert mf({"_type": "playlist", "title": "hotline bling"}) is None


def test_match_filter_captures_skipped_for_reference():
    # Dedup (issue #31): every skipped track is captured (index, artist, title) so the
    # playlist can reference the existing copy — but only on the effective (non-incomplete)
    # call, and never for a track that is downloaded.
    known = {("drake", "hotline bling")}
    captured: list = []
    mf = _make_match_filter(
        lambda a, t: (a.split(",")[0].strip().casefold(), t.casefold()) in known,
        on_skip=lambda idx, a, t: captured.append((idx, a, t)))

    assert mf({"_type": "video", "title": "hotline bling", "artist": "drake",
               "playlist_index": 5}) is not None
    assert captured == [(5, "drake", "hotline bling")]        # skipped → captured with index

    captured.clear()
    assert mf({"_type": "video", "title": "gods plan", "artist": "drake"}) is None
    assert captured == []                                     # a new track is not captured

    captured.clear()
    assert mf({"title": "hotline bling", "artist": "drake"}, incomplete=True) is None
    assert captured == []                                     # incomplete call never captures


def test_credits_artist_distinguishes_own_release_from_label_upload():
    # issue #56: a track counts as the artist's own only when they're a CREDITED performer,
    # not when a compilation/label upload merely names them in the video title.
    own = {"artist": "BCee, Charlotte Haining", "artists": ["BCee", "Charlotte Haining"],
           "channel": "BCee"}
    assert _credits_artist(own, "BCee")
    # label-as-artist (issue's example): artist tag is the label → not credited
    assert not _credits_artist(
        {"artist": "Spearhead Records",
         "title": "BCee & Charlotte Haining - Almost There - Spearhead Records"}, "BCee")
    # third-party reupload: no artist tag, performer only in the title, channel = label
    assert not _credits_artist(
        {"artist": None, "artists": None, "channel": "4U Chill",
         "title": "BCee Featuring Charlotte Haining - Almost There (HD)"}, "BCee")
    # broken SELF-upload on the artist's OWN channel: no artist tag, performer only in the
    # title, channel = the artist → still skipped (channel/uploader are the source, not a credit)
    assert not _credits_artist(
        {"artist": None, "artists": None, "channel": "BCee", "uploader": "BCee",
         "title": "BCee & Lomax - Brazilian Wax - Spearhead Records"}, "BCee")
    # word-boundary, not substring: "Nas" must not match "Nasty"
    assert not _credits_artist({"artist": "Nasty"}, "Nas")
    # multi-word target inside a collab credit still matches
    assert _credits_artist({"artist": "Sub Focus & Wilkinson"}, "Sub Focus")
    # a track with NO real credit tag (only a title) is NOT credited → skipped
    assert not _credits_artist({"title": "mystery"}, "BCee")
    # a blank target never filters
    assert _credits_artist({"artist": "Anyone"}, "")


def test_match_filter_own_artist_skips_foreign_uploads():
    # issue #56: own_artist filter rejects tracks not credited to the artist, keeps their
    # own, defers while incomplete, ignores the playlist envelope, and — unlike a dedup skip —
    # is NOT captured as a reference (it's dropped, not "already on the server").
    captured: list = []
    mf = _make_match_filter(own_artist="BCee",
                            on_skip=lambda idx, a, t: captured.append((idx, a, t)))

    assert mf({"_type": "video", "artist": "BCee, Logistics", "title": "Northpoint"}) is None
    assert mf({"_type": "video", "artist": "Spearhead Records",
               "title": "Almost There - Spearhead Records"}) is not None
    assert mf({"artist": "Spearhead Records", "title": "x"}, incomplete=True) is None
    assert mf({"_type": "playlist", "title": "BCee - Releases"}) is None
    assert captured == []                                   # own_artist drops are never captured


def test_match_filter_own_artist_and_dedup_compose():
    # issue #56 + #31: both filters active. A foreign upload is dropped by own_artist (before
    # the dedup check, so it's not captured); a track credited to the artist that is already on
    # the server is skipped by dedup and captured for a possible m3u reference.
    known = {("bcee", "hurt each other")}
    captured: list = []
    mf = _make_match_filter(
        on_server=lambda a, t: (a.split(",")[0].strip().casefold(), t.casefold()) in known,
        on_skip=lambda idx, a, t: captured.append((idx, a, t)),
        own_artist="BCee")

    # foreign label upload → own_artist reject, not a dedup capture
    assert mf({"_type": "video", "artist": "UKF Drum & Bass",
               "title": "BCee - Hurt Each Other", "playlist_index": 1}) is not None
    assert captured == []
    # credited to BCee AND already on server → dedup skip, captured
    assert mf({"_type": "video", "artist": "BCee", "title": "Hurt Each Other",
               "playlist_index": 2}) is not None
    assert captured == [(2, "BCee", "Hurt Each Other")]
    # credited to BCee and NOT on server → downloads
    assert mf({"_type": "video", "artist": "BCee", "title": "Northpoint"}) is None


def test_build_playlist_manifest_keeps_fresh_reference():
    # A downloaded track plus a cross-folder reference to a DIFFERENT already-present
    # track → both appear, the reference keeping its relative path (issue #31).
    new_entries = [{"index": 1, "name": "0001 - A.mp3", "title": "A", "artist": "X", "dur": 10}]
    refs = [{"index": 2, "name": "../Drake/Views/B.mp3", "title": "B", "artist": "Drake", "dur": -1}]
    manifest = _build_playlist_manifest(None, new_entries, refs, is_sync=False)
    assert {e["name"] for e in manifest} == {"0001 - A.mp3", "../Drake/Views/B.mp3"}


def test_build_playlist_manifest_drops_reference_already_downloaded():
    # Same track both downloaded and "referenced" (raw feat form) → the in-folder
    # download wins, the cross-folder reference is dropped (no double-listing).
    new_entries = [{"index": 1, "name": "0001 - Hotline Bling.mp3",
                    "title": "Hotline Bling", "artist": "Drake", "dur": 10}]
    refs = [{"index": 1, "name": "../Drake/Views/Hotline Bling.mp3",
             "title": "Hotline Bling (feat. X)", "artist": "Drake, X", "dur": -1}]
    manifest = _build_playlist_manifest(None, new_entries, refs, is_sync=False)
    assert [e["name"] for e in manifest] == ["0001 - Hotline Bling.mp3"]


def test_build_playlist_manifest_prior_track_wins_over_reference_on_sync():
    # On a sync, a track already in the prior manifest (bare filename, in-folder) wins
    # over a freshly-resolved cross-folder reference to the same track (issue #31).
    existing = [{"index": 3, "name": "0003 - Song.mp3", "title": "Song", "artist": "Y", "dur": 30}]
    refs = [{"index": 3, "name": "../Y/Album/Song.mp3", "title": "Song", "artist": "Y", "dur": -1}]
    manifest = _build_playlist_manifest(existing, [], refs, is_sync=True)
    assert [e["name"] for e in manifest] == ["0003 - Song.mp3"]


def test_playlist_reference_relpath_frame(tmp_path):
    # The reference frame (issue #31): a track stored relative to the WebDAV base folder
    # is referenced from the playlist folder via posixpath.relpath — a cross-folder track
    # becomes ../…, an in-folder track becomes a bare filename (one code path, both cases).
    assert posixpath.relpath("Drake/Views/Hotline Bling.mp3", "My Mix") \
        == "../Drake/Views/Hotline Bling.mp3"
    assert posixpath.relpath("My Mix/0007 - X.mp3", "My Mix") == "0007 - X.mp3"

    # …and _write_m3u_entries writes that relative path verbatim as the location line.
    ref = posixpath.relpath("Drake/Views/Hotline Bling.mp3", "My Mix")
    m3u = _write_m3u_entries(tmp_path, "My Mix",
                             [{"index": 1, "name": ref, "title": "Hotline Bling",
                               "artist": "Drake", "dur": -1}])
    track_lines = [ln for ln in m3u.read_text(encoding="utf-8").splitlines()
                   if not ln.startswith("#")]
    assert track_lines == ["../Drake/Views/Hotline Bling.mp3"]


def test_merge_manifest_dedupes_by_name_new_wins():
    existing = [{"index": 1, "name": "0001 - A.mp3", "title": "A", "artist": "X", "dur": 10}]
    new = [{"index": 1, "name": "0001 - A.mp3", "title": "A2", "artist": "X", "dur": 11},
           {"index": 2, "name": "0002 - B.mp3", "title": "B", "artist": "Y", "dur": 20}]
    merged = _merge_manifest(existing, new)
    by_name = {e["name"]: e for e in merged}
    assert len(merged) == 2
    assert by_name["0001 - A.mp3"]["title"] == "A2"   # new entry wins on collision


def test_index_from_name():
    assert _index_from_name("0007 - Song.mp3") == 7
    assert _index_from_name("no-index.mp3") == 0


def test_write_m3u_entries_rebuilds_complete_playlist_sorted(tmp_path):
    # A sync rebuilds the full m3u8 from a manifest whose files may not be on local
    # disk; entries are ordered by index and referenced by bare filename (issue #21).
    entries = [
        {"index": 2, "name": "0002 - B.mp3", "title": "B", "artist": "Y", "dur": 20},
        {"index": 1, "name": "0001 - A.mp3", "title": "A", "artist": "X", "dur": 10},
    ]
    m3u = _write_m3u_entries(tmp_path, "My Mix", entries)
    body = m3u.read_text(encoding="utf-8").splitlines()
    assert body[0] == "#EXTM3U"
    track_lines = [ln for ln in body if not ln.startswith("#")]
    assert track_lines == ["0001 - A.mp3", "0002 - B.mp3"]   # sorted by index


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


# --- Artist mode (issue #32) -------------------------------------------------

class _RecordingYDL:
    """A fake yt_dlp.YoutubeDL: returns canned info per URL and records extraction order."""
    def __init__(self, responses: dict):
        self.responses = responses
        self.seen: list[str] = []

    def factory(self):
        responses, seen = self.responses, self.seen

        # Subclass the real YoutubeDL so inherited classmethods (validate_outtmpl, used by
        # parse_options via _extractor_args) keep working; only extract_info is faked.
        class _YDL(yt_dlp.YoutubeDL):
            def __init__(self, opts):
                self.opts = opts                      # intentionally skip super().__init__

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                seen.append(url)
                return responses.get(url, {})

        return _YDL


def test_enumerate_artist_resolves_releases_and_parses(monkeypatch):
    # A bare artist URL is flat-probed for its channel_url, then the /releases tab is
    # enumerated: valid releases parsed (title→"Album" fallback), None/URL-less entries dropped.
    import app.pipeline as pipeline
    responses = {
        "https://music.youtube.com/channel/UCabc": {
            "channel_url": "https://www.youtube.com/channel/UCabc"},
        "https://www.youtube.com/channel/UCabc/releases": {
            "channel": "Rick Astley",
            "entries": [
                {"title": "Album A", "url": "https://x/OLAK5uy_A"},
                {"title": "Album B", "url": "https://x/OLAK5uy_B"},
                None,                                    # dead entry → skipped
                {"url": "https://x/OLAK5uy_C"},          # no title → "Album"
                {"title": "No URL"},                     # no url → skipped
            ],
        },
    }
    rec = _RecordingYDL(responses)
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", rec.factory())

    artist, releases = pipeline.enumerate_artist("https://music.youtube.com/channel/UCabc")

    assert artist == "Rick Astley"
    assert releases == [
        {"title": "Album A", "url": "https://x/OLAK5uy_A"},
        {"title": "Album B", "url": "https://x/OLAK5uy_B"},
        {"title": "Album", "url": "https://x/OLAK5uy_C"},
    ]
    assert rec.seen == ["https://music.youtube.com/channel/UCabc",
                        "https://www.youtube.com/channel/UCabc/releases"]


def test_enumerate_artist_direct_releases_url_and_limit(monkeypatch):
    # A URL that is already a /releases tab is used directly (no channel probe); `limit`
    # caps the release count; the artist name falls back to the title minus " - Releases".
    import app.pipeline as pipeline
    responses = {
        "https://www.youtube.com/channel/UCabc/releases": {
            "title": "Foo Bar - Releases",
            "entries": [{"title": f"R{i}", "url": f"u{i}"} for i in range(5)],
        },
    }
    rec = _RecordingYDL(responses)
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", rec.factory())

    artist, releases = enumerate_artist(
        "https://www.youtube.com/channel/UCabc/releases", limit=2)

    assert artist == "Foo Bar"
    assert [r["url"] for r in releases] == ["u0", "u1"]                    # capped to 2
    assert rec.seen == ["https://www.youtube.com/channel/UCabc/releases"]  # no extra probe


def test_run_artist_download_browser_stages_and_zips_once(monkeypatch, tmp_path):
    # An artist run stages every release through the album path (deliver=False into ONE
    # shared dir, dedup passed through), tolerates a failed release, reports album i/N,
    # and delivers a single combined ZIP under the artist name.
    import app.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_WORK_ROOT", tmp_path / ".work")
    monkeypatch.setattr(pipeline, "enumerate_artist",
                        lambda url, cookiefile=None, limit=0: ("Artist", [
                            {"title": "A", "url": "uA"},
                            {"title": "B", "url": "uB"},
                            {"title": "C", "url": "uC"}]))
    calls = []

    def fake_run_download(**kw):
        calls.append(kw)
        if kw["url"] == "uB":
            raise RuntimeError("boom")                     # one release fails
        d = kw["stage_dir"] / "Artist" / kw["url"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "track.mp3").write_bytes(b"x")                # simulate a staged track
        return Result(delivered=[("Artist", kw["url"], f"Artist/{kw['url']}/track.mp3")],
                      new_track_count=1)

    monkeypatch.setattr(pipeline, "run_download", fake_run_download)
    zipped = {}
    monkeypatch.setattr(pipeline, "_zip_dir",
                        lambda src, zp, name: zipped.update(src=src, name=name))
    albums = []
    reporter = Reporter(on_album=lambda c, t, n: albums.append((c, t, n)))
    sentinel = lambda a, t: False                          # dedup closure

    res = run_artist_download(job_id="job1", url="uArtist", genre="Rap",
                              destination=Destination(type="browser"),
                              reporter=reporter, on_server=sentinel, max_items=0)

    assert len(calls) == 3
    assert all(c["mode"] == "album" and c["deliver"] is False for c in calls)
    assert all(c["stage_dir"] == tmp_path / ".work" / "job1" for c in calls)
    assert all(c["on_server"] is sentinel for c in calls)   # dedup passed to every release
    # each release's title is forced as its album name → distinct per-release folders
    assert [c["album_name"] for c in calls] == ["A", "B", "C"]
    assert res.new_track_count == 2 and len(res.delivered) == 2   # A + C; B skipped
    assert res.zip_name == "Artist.zip"
    assert "übersprungen" in res.summary                    # failure surfaced, run continued
    # album progress: total published up front (0/3), then a monotonic completion count 1..3
    assert albums == [(0, 3, ""), (1, 3, "A"), (2, 3, "B"), (3, 3, "C")]
    assert zipped["name"] == "Artist"
    assert zipped["src"] == tmp_path / ".work" / "job1" / "Artist"   # single artist level


def test_run_artist_download_webdav_uploads_whole_tree_once(monkeypatch, tmp_path):
    import app.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_WORK_ROOT", tmp_path / ".work")
    monkeypatch.setattr(pipeline, "enumerate_artist",
                        lambda url, cookiefile=None, limit=0: ("Artist", [{"title": "A", "url": "uA"}]))

    def fake_run_download(**kw):
        d = kw["stage_dir"] / "Artist" / "A"
        d.mkdir(parents=True, exist_ok=True)
        (d / "t.mp3").write_bytes(b"x")
        return Result(delivered=[("Artist", "A", "Artist/A/t.mp3")], new_track_count=1)

    monkeypatch.setattr(pipeline, "run_download", fake_run_download)
    uploads = []
    monkeypatch.setattr(pipeline, "_upload_tree", lambda dest, root: uploads.append(root))

    res = run_artist_download(job_id="j2", url="u", genre="Rap",
                              destination=Destination(type="webdav"), reporter=Reporter())

    assert uploads == [tmp_path / ".work" / "j2"]           # whole tree uploaded exactly once
    assert res.new_track_count == 1 and len(res.delivered) == 1
    assert res.zip_path is None                             # WebDAV → no ZIP
    assert "WebDAV" in res.summary


def test_run_artist_download_disambiguates_duplicate_release_titles(monkeypatch, tmp_path):
    # Two releases sharing a title must not stage into the same folder (clobbering covers /
    # re-tagging each other): the second gets a disambiguated album name.
    import app.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_WORK_ROOT", tmp_path / ".work")
    monkeypatch.setattr(pipeline, "enumerate_artist",
                        lambda url, cookiefile=None, limit=0: ("Artist", [
                            {"title": "Live", "url": "u1"},
                            {"title": "Live", "url": "u2"}]))
    seen = []

    def fake_run_download(**kw):
        seen.append(kw["album_name"])
        d = kw["stage_dir"] / "Artist" / kw["album_name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "t.mp3").write_bytes(b"x")
        return Result(delivered=[], new_track_count=1)

    monkeypatch.setattr(pipeline, "run_download", fake_run_download)
    monkeypatch.setattr(pipeline, "_zip_dir", lambda src, zp, name: None)

    run_artist_download(job_id="jd", url="u", genre="Rap",
                        destination=Destination(type="browser"), reporter=Reporter())

    assert seen == ["Live", "Live (2)"]                     # second release disambiguated


def test_run_artist_download_passes_own_artist_filter(monkeypatch, tmp_path):
    # issue #56: an artist run forwards own_artist=<resolved name> to every release so
    # compilation/label uploads are filtered per-track — but only with a CONFIDENT name; an
    # unresolved "Artist" fallback (and a "- Topic" suffix) is handled so nothing is over-dropped.
    import app.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_WORK_ROOT", tmp_path / ".work")

    def fake_run_download(**kw):
        d = kw["stage_dir"] / "X" / kw["album_name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "t.mp3").write_bytes(b"x")
        return Result(delivered=[], new_track_count=1)

    monkeypatch.setattr(pipeline, "run_download", fake_run_download)
    monkeypatch.setattr(pipeline, "_zip_dir", lambda src, zp, name: None)

    # A resolved artist (with a "- Topic" channel suffix) → filter on with the bare name.
    calls = []
    monkeypatch.setattr(pipeline, "run_download",
                        lambda **kw: (calls.append(kw), fake_run_download(**kw))[1])
    monkeypatch.setattr(pipeline, "enumerate_artist",
                        lambda url, cookiefile=None, limit=0: ("BCee - Topic",
                                                               [{"title": "A", "url": "uA"}]))
    run_artist_download(job_id="ja", url="u", genre="Rap",
                        destination=Destination(type="browser"), reporter=Reporter())
    assert [c["own_artist"] for c in calls] == ["BCee"]

    # An unresolved fallback name must NOT be used as a filter (would drop everything).
    calls.clear()
    monkeypatch.setattr(pipeline, "enumerate_artist",
                        lambda url, cookiefile=None, limit=0: (pipeline._UNKNOWN_ARTIST,
                                                               [{"title": "A", "url": "uA"}]))
    run_artist_download(job_id="jb", url="u", genre="Rap",
                        destination=Destination(type="browser"), reporter=Reporter())
    assert [c["own_artist"] for c in calls] == [None]


def test_run_artist_download_raises_when_no_releases(monkeypatch, tmp_path):
    import app.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_WORK_ROOT", tmp_path / ".work")
    monkeypatch.setattr(pipeline, "enumerate_artist",
                        lambda url, cookiefile=None, limit=0: ("Artist", []))
    with pytest.raises(RuntimeError):
        run_artist_download(job_id="j3", url="u", genre="Rap",
                            destination=Destination(type="browser"), reporter=Reporter())


def test_run_artist_download_stages_albums_in_parallel(monkeypatch, tmp_path):
    # With album_concurrency > 1 the releases stage concurrently (not strictly one-after-another),
    # results are still fully aggregated, and album progress counts completions up to N — even
    # though releases finish out of order.
    import threading
    import time

    import app.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_WORK_ROOT", tmp_path / ".work")
    monkeypatch.setattr(pipeline, "enumerate_artist",
                        lambda url, cookiefile=None, limit=0: ("Artist", [
                            {"title": "A", "url": "uA"},
                            {"title": "B", "url": "uB"},
                            {"title": "C", "url": "uC"}]))
    lock = threading.Lock()
    live = {"now": 0, "max": 0}

    def fake_run_download(**kw):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.05)                                   # hold the slot so overlap is observable
        with lock:
            live["now"] -= 1
        d = kw["stage_dir"] / "Artist" / kw["album_name"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "t.mp3").write_bytes(b"x")
        return Result(delivered=[("Artist", kw["album_name"], f"Artist/{kw['album_name']}/t.mp3")],
                      new_track_count=1)

    monkeypatch.setattr(pipeline, "run_download", fake_run_download)
    monkeypatch.setattr(pipeline, "_upload_tree", lambda dest, root: None)
    albums = []
    reporter = Reporter(on_album=lambda c, t, n: albums.append((c, t, n)))

    res = run_artist_download(job_id="jp", url="u", genre="Rap",
                              destination=Destination(type="webdav"), reporter=reporter,
                              album_concurrency=3)

    assert live["max"] >= 2                                # at least two releases downloaded at once
    assert res.new_track_count == 3 and len(res.delivered) == 3
    assert albums[0] == (0, 3, "")                         # total published before fan-out
    assert (albums[-1][0], albums[-1][1]) == (3, 3)        # progress reached all-done
    assert {n for _, _, n in albums if n} == {"A", "B", "C"}
