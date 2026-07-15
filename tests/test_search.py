"""In-app YouTube Music search (roadmap 07).

All offline: the `YTMusic` client is replaced with a fake, so no network and no ytmusicapi import
is needed to run these. Covers normalization of real response shapes, the pure URL builders, album
resolution, fail-soft error wrapping, and the invariant that every built URL is one the download
pipeline accepts with the matching mode.
"""
import pytest

from app import search
from app.search import SearchError, SearchResult
from app.sources import is_supported_url, suggest_mode

# Real response shapes (trimmed) from ytmusicapi's own docstring examples.
_SONG = {"resultType": "song", "videoId": "ZrOKjDZOtkA", "title": "Wonderwall",
         "artists": [{"name": "Oasis", "id": "UC..."}],
         "thumbnails": [{"url": "small"}, {"url": "large"}]}
_ALBUM_WITH_PID = {"resultType": "album", "browseId": "MPREb_IInSY5QXXrW",
                   "playlistId": "OLAK5uy_kunInnOpc", "title": "Morning Glory", "artist": "Oasis",
                   "thumbnails": [{"url": "a"}]}
_ALBUM_NO_PID = {"resultType": "album", "browseId": "MPREb_only", "title": "NoPid", "artist": "X"}
_ARTIST = {"resultType": "artist", "browseId": "UCmMUZbaYdNH0bEd1PAlAqsA", "artist": "Oasis"}
_PLAYLIST = {"resultType": "playlist", "browseId": "VLPLK1PkWQlWtnN", "title": "Mix",
             "author": "Tate"}


class FakeYT:
    def __init__(self, per_kind=None, album=None, raises=None):
        self._per_kind = per_kind or {}
        self._album = album or {}
        self._raises = raises
        self.calls = []   # (query, filter) per search — asserts songs-only makes ONE call

    def search(self, query, filter=None, limit=5):  # noqa: A002 - mirror ytmusicapi signature
        self.calls.append((query, filter))
        if self._raises:
            raise self._raises
        return self._per_kind.get(filter, [])

    def get_album(self, browse_id):
        if self._raises:
            raise self._raises
        return self._album


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch):
    # Ensure no real client leaks between tests.
    monkeypatch.setattr(search, "_client_instance", None)
    yield


def _use(fake, monkeypatch):
    monkeypatch.setattr(search, "_client", lambda: fake)


# --- pure URL builders -----------------------------------------------------

def test_url_builders():
    assert search.song_url("abc") == "https://music.youtube.com/watch?v=abc"
    assert search.artist_url("UC123") == "https://music.youtube.com/channel/UC123"
    assert search.album_url("OLAK5uy_x") == "https://music.youtube.com/playlist?list=OLAK5uy_x"
    # playlist strips a leading VL from the browseId
    assert search.playlist_url("VLPLxyz") == "https://music.youtube.com/playlist?list=PLxyz"
    assert search.playlist_url("PLxyz") == "https://music.youtube.com/playlist?list=PLxyz"


# --- normalization ---------------------------------------------------------

def test_search_music_normalizes_all_kinds(monkeypatch):
    _use(FakeYT(per_kind={"songs": [_SONG], "albums": [_ALBUM_WITH_PID],
                          "artists": [_ARTIST], "playlists": [_PLAYLIST]}), monkeypatch)
    by_kind = {r.kind: r for r in search.search_music("oasis")}

    assert by_kind["song"].url == "https://music.youtube.com/watch?v=ZrOKjDZOtkA"
    assert by_kind["song"].artist == "Oasis"
    assert by_kind["song"].thumbnail == "large"                 # largest thumbnail chosen
    assert by_kind["album"].url.endswith("list=OLAK5uy_kunInnOpc")  # in-payload pid used directly
    assert by_kind["artist"].url == "https://music.youtube.com/channel/UCmMUZbaYdNH0bEd1PAlAqsA"
    assert by_kind["playlist"].url == "https://music.youtube.com/playlist?list=PLK1PkWQlWtnN"


def test_album_without_playlistid_defers_resolution(monkeypatch):
    _use(FakeYT(per_kind={"albums": [_ALBUM_NO_PID]}), monkeypatch)
    album = next(r for r in search.search_music("x") if r.kind == "album")
    assert album.url is None                    # must be resolved on click
    assert album.browse_id == "MPREb_only"


def test_normalize_tolerates_missing_fields(monkeypatch):
    _use(FakeYT(per_kind={"songs": [{"resultType": "song", "videoId": "v"}]}), monkeypatch)
    (song,) = [r for r in search.search_music("x") if r.kind == "song"]
    assert song.artist == "" and song.thumbnail is None and song.title == ""


def test_song_without_videoid_is_dropped(monkeypatch):
    _use(FakeYT(per_kind={"songs": [{"resultType": "song", "title": "no id"}]}), monkeypatch)
    assert [r for r in search.search_music("x") if r.kind == "song"] == []


def test_empty_query_short_circuits(monkeypatch):
    _use(FakeYT(raises=RuntimeError("should not be called")), monkeypatch)
    assert search.search_music("   ") == []


# --- album resolution ------------------------------------------------------

def test_resolve_album_url_from_get_album(monkeypatch):
    _use(FakeYT(album={"audioPlaylistId": "OLAK5uy_resolved"}), monkeypatch)
    assert search.resolve_album_url("MPREb_x") == \
        "https://music.youtube.com/playlist?list=OLAK5uy_resolved"


def test_resolve_album_url_missing_id_raises(monkeypatch):
    _use(FakeYT(album={"title": "no playlist id"}), monkeypatch)
    with pytest.raises(SearchError):
        search.resolve_album_url("MPREb_x")


# --- fail-soft error wrapping ----------------------------------------------

def test_search_raises_only_when_all_categories_fail(monkeypatch):
    # Every category errors (e.g. a real outage) → SearchError with no stack/secret leaked.
    _use(FakeYT(raises=ValueError("boom\nTraceback (most recent call last): secret")), monkeypatch)
    with pytest.raises(SearchError) as ei:
        search.search_music("x")
    msg = str(ei.value)
    assert "Traceback" not in msg and "secret" not in msg and len(msg) <= 200


def test_client_init_failure_is_wrapped(monkeypatch):
    def _boom():
        raise RuntimeError("no ytmusicapi")
    monkeypatch.setattr(search, "_client", _boom)
    with pytest.raises(SearchError):
        search.search_music("x")


class _PartialYT:
    """One good song + one malformed song item; the albums category raises entirely."""
    def search(self, query, filter=None, limit=5):  # noqa: A002
        if filter == "songs":
            # 2nd item's `artists` is a list of non-dicts → `_first_artist` raises → item skipped.
            return [_SONG, {"resultType": "song", "videoId": "bad", "artists": ["not-a-dict"]}]
        if filter == "albums":
            raise ValueError("album shape drift")   # whole category fails
        return []


def test_partial_drift_degrades_gracefully(monkeypatch):
    _use(_PartialYT(), monkeypatch)
    results = search.search_music("x")     # must NOT raise despite album failure + a bad item
    songs = [r for r in results if r.kind == "song"]
    assert len(songs) == 1 and songs[0].title == "Wonderwall"  # good item survived, bad one skipped
    assert not any(r.kind == "album" for r in results)         # failed category dropped, not fatal


# --- pipeline compatibility (the "downloadable as-is" guarantee) -----------

def test_every_built_url_is_supported_and_suggests_matching_mode(monkeypatch):
    _use(FakeYT(per_kind={"songs": [_SONG], "albums": [_ALBUM_WITH_PID],
                          "artists": [_ARTIST], "playlists": [_PLAYLIST]}), monkeypatch)
    expected = {"song": "single", "album": "album", "artist": "artist", "playlist": "playlist"}
    for r in search.search_music("oasis"):
        assert r.url is not None
        assert is_supported_url(r.url), r.url
        assert suggest_mode(r.url) == expected[r.kind], (r.kind, r.url)


# --- songs-only search (roadmap 12 batch matching) -------------------------

def test_search_songs_makes_one_songs_call(monkeypatch):
    fake = FakeYT(per_kind={"songs": [_SONG]})
    _use(fake, monkeypatch)
    results = search.search_songs("wonderwall", limit=3)
    assert [r.kind for r in results] == ["song"]
    assert results[0].url == "https://music.youtube.com/watch?v=ZrOKjDZOtkA"
    assert fake.calls == [("wonderwall", "songs")]   # exactly ONE call, songs filter only


def test_search_songs_empty_query_short_circuits(monkeypatch):
    fake = FakeYT(raises=RuntimeError("should not be called"))
    _use(fake, monkeypatch)
    assert search.search_songs("  ") == []
    assert fake.calls == []


def test_search_songs_fails_soft(monkeypatch):
    _use(FakeYT(raises=ValueError("down")), monkeypatch)
    with pytest.raises(search.SearchError):
        search.search_songs("x")
