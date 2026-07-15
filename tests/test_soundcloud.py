"""SoundCloud support (roadmap 06) — all offline.

Covers the four source-gated pipeline extensions and the YouTube regressions that prove they stay
byte-identical when the SoundCloud flags are off: uploader-as-credit (`trust_uploader`), the
artist-profile enumerator, the cover-URL upgrade, and the Go+ preview predicate + accounting.
"""
import app.pipeline as pipeline
from app.pipeline import (
    _artist_credit_text,
    _credits_artist,
    _enumerate_artist_soundcloud,
    _is_preview,
    _make_match_filter,
    _pick_soundcloud_cover,
    enumerate_artist,
)
from app.sources import SOUNDCLOUD


# --- crediting: uploader trusted only when trust_uploader=True -------------

def test_credits_artist_trusts_uploader_only_when_enabled():
    # A SoundCloud track's only performer signal is the uploader (no structured artist tag).
    info = {"uploader": "Forss", "title": "Flickermood"}
    assert _credits_artist(info, "Forss", trust_uploader=True) is True
    # Without the flag (the YouTube default) the uploader is NOT a credit → not matched.
    assert _credits_artist(info, "Forss", trust_uploader=False) is False
    assert _credits_artist(info, "Forss") is False   # default arg == YouTube behaviour


def test_credits_artist_real_tag_matches_either_way():
    # A real artist tag counts regardless of the flag — trust_uploader only ADDS uploader/channel.
    info = {"artist": "Forss", "uploader": "Some Label"}
    assert _credits_artist(info, "Forss", trust_uploader=True) is True
    assert _credits_artist(info, "Forss", trust_uploader=False) is True


def test_artist_credit_text_uploader_gate():
    info = {"uploader": "Forss", "channel": "Forss Official"}
    assert _artist_credit_text(info) == ""                          # uploader/channel excluded
    blob = _artist_credit_text(info, trust_uploader=True)
    assert "forss" in blob and "forss official" in blob


# --- cover URL upgrade -----------------------------------------------------

def test_pick_soundcloud_cover_upgrades_to_500():
    base = "https://i1.sndcdn.com/artworks-000067273316-smsiqx"
    for variant in ("large", "t300x300", "crop", "original", "t500x500"):
        got = _pick_soundcloud_cover([{"url": f"{base}-{variant}.jpg"}])
        assert got == f"{base}-t500x500.jpg", variant


def test_pick_soundcloud_cover_falls_back_to_square_picker():
    # A thumbnail with no SoundCloud size token → generic square picker (largest w==h).
    thumbs = [{"url": "https://x/y.png?sqp=a", "width": 100, "height": 100}]
    assert _pick_soundcloud_cover(thumbs) == "https://x/y.png?sqp=a"
    assert _pick_soundcloud_cover([]) is None


# --- Go+ preview predicate -------------------------------------------------

def test_is_preview_predicate_table():
    assert _is_preview({"duration": 30.0, "full_duration": 213.0}) is True     # 30s snippet
    assert _is_preview({"duration": 30.0, "full_duration": 32.0}) is True       # short-track preview
    assert _is_preview({"duration": 213.886, "full_duration": None}) is False  # full track
    assert _is_preview({"duration": 213, "full_duration": 213}) is False       # equal → full
    assert _is_preview({"duration": 213.5, "full_duration": 213.9}) is False    # sub-1.5s rounding
    assert _is_preview({"duration": 200}) is False                             # YouTube-like
    assert _is_preview({}) is False
    assert _is_preview({"duration": "x", "full_duration": "y"}) is False       # unparseable


def test_match_filter_records_and_skips_preview():
    seen, preview = set(), set()
    mf = _make_match_filter(on_seen=seen.add, on_preview=preview.add)
    # A preview is skipped (returns a reason) + recorded, but NOT marked expected (so the
    # download-retry loop never chases an impossible track).
    reason = mf({"id": "1", "title": "Snippet", "duration": 30, "full_duration": 200})
    assert reason and "Vorschau" in reason
    assert preview == {"1"} and seen == set()
    # A full track passes the filter and is recorded as expected.
    assert mf({"id": "2", "title": "Full", "duration": 200, "full_duration": None}) is None
    assert seen == {"2"} and preview == {"1"}


def test_match_filter_preview_check_runs_after_credit_and_dedup():
    # A preview by a FOREIGN artist (not own_artist) is dropped as not-credited, NOT counted as a
    # preview — the preview check runs after the credit filter (roadmap 06 review finding #2).
    preview = set()
    mf = _make_match_filter(own_artist="Forss", trust_uploader=True, on_preview=preview.add)
    reason = mf({"id": "9", "title": "Someone Else - Snippet", "uploader": "Rando",
                 "duration": 30, "full_duration": 200})
    assert reason and "nicht vom Künstler" in reason and preview == set()

    # A preview already on the server is skipped as existing, not counted as a preview.
    preview.clear()
    mf2 = _make_match_filter(on_server=lambda a, t: True, on_preview=preview.add)
    reason2 = mf2({"id": "8", "title": "Old", "artist": "Forss",
                   "duration": 30, "full_duration": 200})
    assert reason2 and "schon auf dem Server" in reason2 and preview == set()


def test_match_filter_without_on_preview_ignores_duration():
    # YouTube path passes no on_preview → the preview branch is inert (parity).
    seen = set()
    mf = _make_match_filter(on_seen=seen.add)
    assert mf({"id": "1", "title": "x", "duration": 30, "full_duration": 200}) is None
    assert seen == {"1"}


# --- artist enumeration ----------------------------------------------------

class _FakeYDL:
    """Minimal YoutubeDL stand-in: dispatches extract_info by URL suffix."""

    _RESPONSES = {
        "https://soundcloud.com/forss": {
            "uploader": "Forss", "uploader_url": "https://soundcloud.com/forss"},
        "https://soundcloud.com/forss/albums": {"uploader": "Forss", "entries": [
            {"title": "Soulhack", "url": "https://soundcloud.com/forss/sets/soulhack"}]},
        "https://soundcloud.com/forss/tracks": {"uploader": "Forss", "entries": [
            {"title": "Flickermood", "url": "https://soundcloud.com/forss/flickermood"},
            # a track that is ALSO in the album (same set url) — deduped by url
            {"title": "Soulhack", "url": "https://soundcloud.com/forss/sets/soulhack"}]},
    }

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return dict(self._RESPONSES.get(url.rstrip("/"), {}))


def test_enumerate_artist_soundcloud_combines_albums_and_tracks(monkeypatch):
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", _FakeYDL)
    artist, releases = _enumerate_artist_soundcloud(
        "https://soundcloud.com/forss", opts={}, limit=0)
    assert artist == "Forss"
    urls = [r["url"] for r in releases]
    # Album first, then the loose track; the duplicate set url from /tracks is dropped.
    assert urls == ["https://soundcloud.com/forss/sets/soulhack",
                    "https://soundcloud.com/forss/flickermood"]


def test_enumerate_artist_dispatches_soundcloud(monkeypatch):
    # enumerate_artist(source=SOUNDCLOUD) routes to the SoundCloud branch (not the YT /releases).
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", _FakeYDL)
    artist, releases = enumerate_artist(
        "https://soundcloud.com/forss/tracks", source=SOUNDCLOUD)
    assert artist == "Forss" and len(releases) == 2


def test_enumerate_artist_soundcloud_respects_limit(monkeypatch):
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", _FakeYDL)
    _, releases = _enumerate_artist_soundcloud(
        "https://soundcloud.com/forss", opts={}, limit=1)
    assert len(releases) == 1
