"""Spotify / Apple playlist import (roadmap 13).

All offline: httpx and the Spotify token are monkeypatched; no network. Covers URL detection +
SSRF host validation, the Spotify parser (pagination, is_local/null skipping, artist join), the
Apple parser (serialized-server-data extraction + fail-soft), and the m3u recreation
(`pipeline._write_import_m3u`: source order, cross-folder relpaths, dropped-unresolved).
"""
import json
from pathlib import Path

import pytest

import app.playlist_import as pi
from app import pipeline
from app.config import settings
from app.library_index import track_key
from app.pipeline import PlaylistSpec
from app.playlist_import import AppleParseError, ImportedTrack, SpotifyError, detect_import_url


# --- URL detection + SSRF --------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
     ("spotify", "playlist", "37i9dQZF1DXcBWIGoYBM5M")),
    ("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3?si=x",
     ("spotify", "album", "1DFixLWuPkv3KT3TnV35m3")),
    ("https://open.spotify.com/intl-de/playlist/ABC123", ("spotify", "playlist", "ABC123")),
    ("https://music.apple.com/us/playlist/hits/pl.f4d106fed2bd4114",
     ("apple", "playlist", "pl.f4d106fed2bd4114")),
    ("https://music.apple.com/de/album/foo/1618983263", ("apple", "album", "1618983263")),
    ("https://api.spotify.com.evil.tld/playlist/x", None),   # lookalike host → rejected
    ("https://evil.com/playlist/x", None),
    ("ftp://open.spotify.com/playlist/x", None),             # non-http scheme
    ("https://open.spotify.com/artist/x", None),             # unsupported kind
    ("garbage", None),
])
def test_detect_import_url(url, expected):
    assert detect_import_url(url) == expected


# --- Spotify parser --------------------------------------------------------

@pytest.fixture
def spotify_on(monkeypatch):
    monkeypatch.setattr(settings, "spotify_client_id", "id", raising=False)
    monkeypatch.setattr(settings, "spotify_client_secret", "secret", raising=False)
    monkeypatch.setattr(pi, "_spotify_token", lambda: "TOKEN")
    return monkeypatch


def test_spotify_playlist_pagination_and_skips(spotify_on):
    pages = {
        "https://api.spotify.com/v1/playlists/PL": {"name": "My Mix", "tracks": {
            "items": [
                {"track": {"name": "A", "artists": [{"name": "A1"}, {"name": "A2"}],
                           "album": {"name": "Alb1"}}},
                {"track": None},                                  # unavailable → skipped
                {"track": {"name": "Local", "is_local": True, "artists": []}},   # local → skipped
            ],
            "next": "https://api.spotify.com/v1/playlists/PL/tracks?offset=100"}},
        "https://api.spotify.com/v1/playlists/PL/tracks?offset=100": {
            "items": [{"track": {"name": "B", "artists": [{"name": "B1"}],
                                 "album": {"name": "Alb2"}}}],
            "next": None},
    }
    spotify_on.setattr(pi, "_spotify_get", lambda url, headers: pages[url])
    pl = pi.fetch_spotify("playlist", "PL")
    assert pl.name == "My Mix" and pl.source == "spotify"
    assert [(t.artist, t.title, t.album) for t in pl.tracks] == [
        ("A1, A2", "A", "Alb1"), ("B1", "B", "Alb2")]   # paginated, joined, skips applied


def test_spotify_pagination_ignores_offhost_next(spotify_on):
    # A tampered `next` pointing off api.spotify.com must NOT be followed (token-leak guard).
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        if url.endswith("/playlists/PL"):
            return {"name": "M", "tracks": {
                "items": [{"track": {"name": "A", "artists": [{"name": "X"}]}}],
                "next": "https://evil.tld/steal?token=1"}}
        raise AssertionError(f"followed off-host next: {url}")

    spotify_on.setattr(pi, "_spotify_get", fake_get)
    pl = pi.fetch_spotify("playlist", "PL")
    assert [t.title for t in pl.tracks] == ["A"]          # first page kept
    assert calls == ["https://api.spotify.com/v1/playlists/PL"]   # off-host next NOT fetched


def test_spotify_album_uses_album_name(spotify_on):
    spotify_on.setattr(pi, "_spotify_get", lambda url, headers: {
        "name": "The Album", "artists": [{"name": "Band"}],
        "tracks": {"items": [{"name": "Song", "artists": [{"name": "Band"}]}], "next": None}})
    pl = pi.fetch_spotify("album", "AL")
    assert pl.tracks[0].album == "The Album"


def test_spotify_unconfigured_raises(monkeypatch):
    monkeypatch.setattr(settings, "spotify_client_id", None, raising=False)
    monkeypatch.setattr(settings, "spotify_client_secret", None, raising=False)
    with pytest.raises(SpotifyError):
        pi._spotify_token()


# --- Apple parser ----------------------------------------------------------

def _apple_html(tracks) -> str:
    data = [{"data": {"sections": [{"items": [
        {"@type": "song", "title": t[1], "artistName": t[0]} for t in tracks]}]}}]
    return ('<html><head><title>Road Trip – Apple Music</title></head><body>'
            '<script id="serialized-server-data">' + json.dumps(data) + '</script></body></html>')


def test_apple_parses_tracklist_and_name(monkeypatch):
    html = _apple_html([("Burial", "Archangel"), ("Aphex Twin", "Xtal")])
    monkeypatch.setattr(pi.httpx, "get",
                        lambda *a, **k: type("R", (), {"text": html,
                                                       "raise_for_status": lambda self: None})())
    pl = pi.fetch_apple("https://music.apple.com/us/playlist/x/pl.abc")
    assert pl.name == "Road Trip"
    assert [(t.artist, t.title) for t in pl.tracks] == [("Burial", "Archangel"),
                                                        ("Aphex Twin", "Xtal")]


def test_apple_missing_data_raises(monkeypatch):
    monkeypatch.setattr(pi.httpx, "get",
                        lambda *a, **k: type("R", (), {"text": "<html>no data here</html>",
                                                       "raise_for_status": lambda self: None})())
    with pytest.raises(AppleParseError):
        pi.fetch_apple("https://music.apple.com/us/playlist/x/pl.abc")


# --- m3u recreation --------------------------------------------------------

def test_write_import_m3u_order_and_relpaths(tmp_path):
    spec = PlaylistSpec(name="My Mix", folder_id="import-abc1234567",
                        tracks=[("Burial", "Archangel"), ("Aphex Twin", "Xtal"),
                                ("Nobody", "Missing")])
    delivered = [("Aphex Twin", "Xtal", "Aphex Twin/SAW/02 - Xtal.mp3")]         # fresh
    index = {track_key("Archangel", "Burial"): "Burial/Untrue/05 - Archangel.mp3"}  # on server
    pipeline._write_import_m3u(tmp_path, spec, delivered, index)

    m3u = next(tmp_path.rglob("*.m3u8"))
    assert m3u.parent.name == "My Mix [import-abc1234567]"
    lines = [ln for ln in m3u.read_text().splitlines() if not ln.startswith("#")]
    # Source order preserved (Burial before Aphex Twin); "Nobody - Missing" unresolved → absent.
    assert lines == ["../Burial/Untrue/05 - Archangel.mp3", "../Aphex Twin/SAW/02 - Xtal.mp3"]


def test_write_import_m3u_no_resolvable_tracks_writes_nothing(tmp_path):
    spec = PlaylistSpec(name="X", folder_id="import-000000000000", tracks=[("A", "B")])
    pipeline._write_import_m3u(tmp_path, spec, delivered=[], index_paths={})
    assert list(tmp_path.rglob("*.m3u8")) == []
