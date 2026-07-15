"""Bandcamp support (roadmap 11) — all offline.

Covers the source-gated pipeline extensions: the cover-URL size upgrade (Bandcamp thumbnails have
no dimensions, so `pick_square_cover` can't pick them), the artist-page enumerator (flat `/music`
+ slug titles + real-name probe), and the crediting reuse (uploader trusted like SoundCloud).
"""
import app.pipeline as pipeline
from app.pipeline import (
    _bandcamp_slug_title,
    _credits_artist,
    _enumerate_artist_bandcamp,
    _pick_bandcamp_cover,
    _probe_meta,
    enumerate_artist,
)
from app.sources import BANDCAMP, YOUTUBE


# --- cover URL size upgrade ------------------------------------------------

def test_pick_bandcamp_cover_upgrades_size_code():
    base = "https://f4.bcbits.com/img/a3390257927"
    for code in ("_5", "_16", "_7", "_0", "_10"):
        assert _pick_bandcamp_cover([{"url": f"{base}{code}.jpg"}]) == f"{base}_10.jpg", code
    # png variant is upgraded too, extension preserved.
    assert _pick_bandcamp_cover([{"url": f"{base}_5.png"}]) == f"{base}_10.png"


def test_pick_bandcamp_cover_falls_back_without_size_code():
    thumbs = [{"url": "https://x/y.jpg", "width": 700, "height": 700}]
    assert _pick_bandcamp_cover(thumbs) == "https://x/y.jpg"   # generic square picker
    assert _pick_bandcamp_cover([]) is None


# --- crediting: uploader trusted (page owner is the artist) ----------------

def test_bandcamp_credits_trust_uploader():
    info = {"uploader": "C418"}
    assert _credits_artist(info, "C418", trust_uploader=True) is True    # page owner
    assert _credits_artist(info, "C418", trust_uploader=False) is False  # YouTube default


# --- slug display title ----------------------------------------------------

def test_bandcamp_slug_title():
    assert _bandcamp_slug_title("https://c418.bandcamp.com/album/minecraft-volume-alpha") \
        == "minecraft volume alpha"
    assert _bandcamp_slug_title("https://c418.bandcamp.com/track/key") == "key"
    assert _bandcamp_slug_title("https://c418.bandcamp.com/album/") == "album"


# --- artist enumeration ----------------------------------------------------

class _FakeYDL:
    """YoutubeDL stand-in dispatching extract_info by URL; flat /music has no per-entry titles."""

    def __init__(self, opts):
        self.flat = bool(opts.get("extract_flat"))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if url.endswith("/music"):
            return {"title": "Discography of c418", "id": "c418", "entries": [
                {"url": "https://c418.bandcamp.com/album/minecraft-volume-alpha"},
                {"url": "https://c418.bandcamp.com/album/minecraft-volume-beta"},
                {"url": "https://c418.bandcamp.com/album/minecraft-volume-alpha"},  # dup url
            ]}
        # non-flat probe of the first release → carries the clean artist tag
        return {"entries": [{"artist": "C418", "album": "Minecraft - Volume Alpha", "track": "Key"}]}


def test_enumerate_artist_bandcamp(monkeypatch):
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", _FakeYDL)
    artist, releases = _enumerate_artist_bandcamp(
        "https://c418.bandcamp.com", opts={"extract_flat": True}, limit=0)
    # Real artist name recovered from the first release probe (not the lowercase subdomain).
    assert artist == "C418"
    # Duplicate URL deduped, slug-derived titles, order preserved.
    assert [(r["title"], r["url"]) for r in releases] == [
        ("minecraft volume alpha", "https://c418.bandcamp.com/album/minecraft-volume-alpha"),
        ("minecraft volume beta", "https://c418.bandcamp.com/album/minecraft-volume-beta"),
    ]


def test_enumerate_artist_dispatches_bandcamp(monkeypatch):
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", _FakeYDL)
    artist, releases = enumerate_artist(
        "https://c418.bandcamp.com/music", source=BANDCAMP)
    assert artist == "C418" and len(releases) == 2


def test_enumerate_artist_bandcamp_respects_limit(monkeypatch):
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", _FakeYDL)
    _, releases = _enumerate_artist_bandcamp(
        "https://c418.bandcamp.com", opts={"extract_flat": True}, limit=1)
    assert len(releases) == 1


# --- _probe_meta album fallback (fix #1: standalone tracks get their own folder) -----------

def _probe_yt(payload):
    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return payload
    return FakeYDL


def test_probe_meta_bandcamp_track_falls_back_to_title(monkeypatch):
    # A standalone Bandcamp /track/ has no `album` tag → named by its own title, so several
    # loose singles don't all collapse into one shared "Unbekannt Album" folder (review finding #1).
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL",
                        _probe_yt({"artist": "C418", "title": "Wet Hands"}))  # no album, no entries
    artist, album = _probe_meta("https://c418.bandcamp.com/track/wet-hands",
                                is_album=True, source=BANDCAMP)
    assert artist == "C418" and album == "Wet Hands"


def test_probe_meta_youtube_no_album_stays_none(monkeypatch):
    # Parity guard: a YouTube album whose first track lacks an `album` tag must NOT fold in the
    # playlist title (that would change the frozen ID3 album output) — album stays None.
    monkeypatch.setattr(pipeline, "_extractor_args", lambda *a, **k: {})  # skip parse_options
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL",
                        _probe_yt({"title": "Some Playlist",
                                   "entries": [{"uploader": "X", "title": "T"}]}))
    artist, album = _probe_meta("https://music.youtube.com/playlist?list=PLx",
                                is_album=True, source=YOUTUBE)
    assert album is None
