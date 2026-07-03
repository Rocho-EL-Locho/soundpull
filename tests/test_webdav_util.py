"""WebDAV path encoding: `#`/`?` in track/album names must not abort uploads.

Regression for the artist-download crash `InvalidURL: Invalid URL component 'path'`:
webdav4 feeds each resource path into httpx's `URL.copy_with(path=…)`, whose path regex
is `[^?#]*`, so a literal `#`/`?` in a folder or file name (both legal on disk and on the
server) blew up the WebDAV client before any HTTP happened. `_SafePathClient` pre-encodes
those chars so the upload proceeds.
"""
from httpx import URL

from app.webdav_util import _encode_webdav_path, _SafePathClient, make_client


def _client() -> _SafePathClient:
    return _SafePathClient(
        base_url="https://cloud.example.com/remote.php/dav/files/user",
        auth=("u", "p"),
    )


def test_encoder_is_noop_for_ordinary_paths():
    """Parity guard: names without %/#/? are passed through byte-for-byte."""
    for p in [
        "",
        "Music/UKF Drum & Bass/Album/01 - Track.mp3",
        "Folder/Álbum (2020) [Deluxe]/01 - Song feat. X.mp3",
    ]:
        assert _encode_webdav_path(p) == p


def test_encoder_escapes_hash_question_and_percent_but_not_slash():
    assert _encode_webdav_path("Best Of #1/Track #2.mp3") == "Best Of %231/Track %232.mp3"
    assert _encode_webdav_path("What? EP/x.mp3") == "What%3F EP/x.mp3"
    # `%` is escaped first so an existing %XX in a name isn't misread as an escape.
    assert _encode_webdav_path("50%/x.mp3") == "50%25/x.mp3"


def test_join_url_no_longer_raises_on_hash_or_question():
    """The exact paths that previously raised InvalidURL now build a valid URL."""
    c = _client()
    for p in [
        "Music/Artist/Best Of #1/Track.mp3",
        "Music/Artist/What? EP/Track #2.mp3",
    ]:
        url = c.join_url(p)  # would have raised InvalidURL before the fix
        assert "%23" in str(url) or "%3F" in str(url)


def test_request_and_response_paths_decode_to_same_key():
    """ls()/info() key off `URL.path`; encoding must not break that match.

    httpx decodes `.path`, so the encoded request URL and the server's percent-encoded
    href normalise to the same key.
    """
    c = _client()
    request_url = c.join_url("Music/Artist/Best Of #1")
    href = URL(
        "https://cloud.example.com/remote.php/dav/files/user/Music/Artist/Best%20Of%20%231/"
    )
    assert request_url.path == href.path.rstrip("/")


def test_make_client_returns_safe_path_client():
    c = make_client("https://cloud.example.com/dav", "u", "p")
    assert isinstance(c, _SafePathClient)
