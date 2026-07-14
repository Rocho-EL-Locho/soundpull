"""Source registry (roadmap feature 02): URL detection + mode suggestion + extensibility.

These guard the two user-visible behaviours (a URL resolves to the right source, and
pre-selects the right mode) and the architectural promise: adding a source is a single
registry entry, and a non-YouTube source derives its own yt-dlp flags.
"""
import app.sources as sources
from app.pipeline import _ALBUM_FLAGS, _apply_source
from app.sources import (
    SourceSpec,
    YOUTUBE,
    detect_source,
    is_supported_url,
    suggest_mode,
)


def test_detect_source_accepts_youtube_hosts():
    for url in (
        "https://music.youtube.com/watch?v=abc",
        "https://www.youtube.com/playlist?list=x",
        "https://m.youtube.com/watch?v=abc",
        "https://youtube.com/watch?v=abc",
        "https://youtu.be/abc",
    ):
        assert detect_source(url) is YOUTUBE, url
    assert is_supported_url("https://music.youtube.com/watch?v=abc")


def test_detect_source_rejects_unknown_and_garbage():
    # Unknown host, substring-only lookalike, wrong scheme, garbage → None, never an exception.
    for url in (
        "https://soundcloud.com/artist/track",
        "https://youtube.com.evil.com/x",   # not a real youtube host
        "https://evil.com/youtube.com",     # substring only
        "file:///etc/passwd",               # wrong scheme
        "not a url at all",
        "",
    ):
        assert detect_source(url) is None, url
        assert not is_supported_url(url)


def test_suggest_mode_youtube_table():
    cases = {
        # album playlist id (OLAK5uy_…) → album, whether on a watch or playlist URL
        "https://music.youtube.com/watch?v=abc&list=OLAK5uy_abcdef": "album",
        "https://music.youtube.com/playlist?list=OLAK5uy_abcdef": "album",
        # a plain watch / short link without a list → single
        "https://music.youtube.com/watch?v=abc": "single",
        "https://youtu.be/dQw4w9WgXcQ": "single",
        # a non-album list id → playlist
        "https://music.youtube.com/playlist?list=PLabcdef": "playlist",
        "https://www.youtube.com/watch?v=abc&list=RDabcdef": "playlist",
        # channel / handle → artist
        "https://music.youtube.com/channel/UC1234567890": "artist",
        "https://www.youtube.com/@someartist": "artist",
        # nothing recognisable → no suggestion (keep the current toggle)
        "https://www.youtube.com/": None,
    }
    for url, expected in cases.items():
        assert suggest_mode(url) == expected, url


def test_suggest_mode_unknown_host_is_none():
    # An unknown host has no source, so there is no mode to suggest.
    assert suggest_mode("https://soundcloud.com/artist/sets/x") is None


def test_adding_a_source_is_just_a_registry_entry(monkeypatch):
    """A hypothetical new source round-trips detection + flag derivation via the registry."""
    dummy = SourceSpec(
        key="dummy",
        label="Dummy",
        extractor_args="dummy:variant=test",
        supports_cookies=False,
        supports_pot=False,
        supports_artist=False,
        cover_square_crop=False,
        matches=lambda u: "dummy.test" in u,
        suggest_mode=lambda u: "single",
    )
    monkeypatch.setattr(sources, "_REGISTRY", (dummy, YOUTUBE))

    # Detection routes the dummy host to the dummy spec; YouTube still works.
    assert detect_source("https://dummy.test/track") is dummy
    assert detect_source("https://music.youtube.com/watch?v=abc") is YOUTUBE
    assert suggest_mode("https://dummy.test/track") == "single"

    # Flag derivation swaps YouTube's extractor-args for the dummy's — no youtube: args leak.
    flags = _apply_source(_ALBUM_FLAGS, dummy)
    assert "youtube:player_client=android_vr,mweb" not in flags
    assert "dummy:variant=test" in flags
    # exactly one --extractor-args pair remains (the replacement, not both)
    assert flags.count("--extractor-args") == 1


def test_apply_source_drops_extractor_args_when_source_has_none(monkeypatch):
    """A source with no extractor_args derives a list without any --extractor-args pair."""
    bare = SourceSpec(
        key="bare", label="Bare", extractor_args=None,
        supports_cookies=False, supports_pot=False, supports_artist=False,
        cover_square_crop=True, matches=lambda u: False, suggest_mode=lambda u: None,
    )
    flags = _apply_source(_ALBUM_FLAGS, bare)
    assert "--extractor-args" not in flags
    assert "youtube:player_client=android_vr,mweb" not in flags
