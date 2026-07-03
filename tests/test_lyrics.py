"""Best-effort synced-lyrics fetch + `.lrc` sidecar (issue #43).

The pipeline must never fail a job over lyrics, so these pin the contract: a hit returns
the LRC text, every miss/error path returns None (never raises), instrumentals/empties are
skipped, synced is preferred over plain, results are cached per track, and `write_lrc_for`
drops a UTF-8 sidecar next to the track (using the PRIMARY artist for the lookup).
"""
import shutil
import subprocess

import pytest

import app.lyrics as lyrics

_SYNCED = "[00:12.34]hello\n[00:15.00]world"


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache():
    # fetch_synced_lyrics is lru_cached (per-track cache) — isolate every test.
    lyrics.fetch_synced_lyrics.cache_clear()
    yield
    lyrics.fetch_synced_lyrics.cache_clear()


def test_get_returns_synced_lyrics(monkeypatch):
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return _Resp(200, {"syncedLyrics": _SYNCED, "plainLyrics": "hello world"})

    monkeypatch.setattr(lyrics.httpx, "get", fake_get)
    assert lyrics.fetch_synced_lyrics("A", "Song", "Album", 200) == _SYNCED
    assert seen and seen[0].endswith("/api/get")  # exact match tried first


def test_prefers_synced_over_plain(monkeypatch):
    monkeypatch.setattr(lyrics.httpx, "get",
                        lambda url, **kw: _Resp(200, {"syncedLyrics": _SYNCED, "plainLyrics": "plain"}))
    assert lyrics.fetch_synced_lyrics("A", "Song", None, 200) == _SYNCED


def test_falls_back_to_plain_when_no_synced(monkeypatch):
    monkeypatch.setattr(lyrics.httpx, "get",
                        lambda url, **kw: _Resp(200, {"syncedLyrics": "", "plainLyrics": "plain only"}))
    assert lyrics.fetch_synced_lyrics("A", "Song", None, 200) == "plain only"


def test_instrumental_returns_none(monkeypatch):
    monkeypatch.setattr(lyrics.httpx, "get",
                        lambda url, **kw: _Resp(200, {"instrumental": True, "syncedLyrics": None}))
    assert lyrics.fetch_synced_lyrics("A", "Song", None, 200) is None


def test_404_get_falls_back_to_search(monkeypatch):
    def fake_get(url, **kw):
        if url.endswith("/api/get"):
            return _Resp(404, {"code": 404})
        return _Resp(200, [{"syncedLyrics": _SYNCED}])

    monkeypatch.setattr(lyrics.httpx, "get", fake_get)
    assert lyrics.fetch_synced_lyrics("A", "Song", None, 200) == _SYNCED


def test_no_duration_uses_get_without_duration(monkeypatch):
    seen = []

    def fake_get(url, **kw):
        seen.append((url, kw.get("params", {})))
        return _Resp(200, {"syncedLyrics": _SYNCED})

    monkeypatch.setattr(lyrics.httpx, "get", fake_get)
    assert lyrics.fetch_synced_lyrics("A", "Song") == _SYNCED
    # No duration → the exact-match get is skipped, but the canonical /api/get (no duration)
    # is tried and already hits, so the slow fuzzy search is never reached.
    assert len(seen) == 1
    url, params = seen[0]
    assert url.endswith("/api/get") and "duration" not in params


def test_duration_mismatch_recovers_via_get_without_duration(monkeypatch):
    # The core fix: an exact /api/get with a mismatched duration 404s, but the second
    # /api/get WITHOUT duration recovers the lyrics — no fall-through to the slow search.
    seen = []

    def fake_get(url, **kw):
        params = kw.get("params", {})
        seen.append((url, "duration" in params))
        if url.endswith("/api/get") and "duration" in params:
            return _Resp(404, {"code": 404})          # exact match fails (wrong duration)
        if url.endswith("/api/get"):
            return _Resp(200, {"syncedLyrics": _SYNCED})  # canonical match succeeds
        raise AssertionError("should not reach /api/search")

    monkeypatch.setattr(lyrics.httpx, "get", fake_get)
    assert lyrics.fetch_synced_lyrics("A", "Song", "Album", 999) == _SYNCED
    assert seen == [("https://lrclib.net/api/get", True),
                    ("https://lrclib.net/api/get", False)]


def test_network_error_returns_none(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(lyrics.httpx, "get", boom)
    assert lyrics.fetch_synced_lyrics("A", "Song", None, 200) is None  # best-effort, never raises


def test_missing_artist_or_title_returns_none(monkeypatch):
    def boom(url, **kw):  # must not even hit the network
        raise AssertionError("should not fetch without artist/title")

    monkeypatch.setattr(lyrics.httpx, "get", boom)
    assert lyrics.fetch_synced_lyrics("", "Song", None, 200) is None
    assert lyrics.fetch_synced_lyrics("A", "", None, 200) is None


def test_result_is_cached(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(200, {"syncedLyrics": _SYNCED})

    monkeypatch.setattr(lyrics.httpx, "get", fake_get)
    lyrics.fetch_synced_lyrics("A", "Song", "Album", 200)
    lyrics.fetch_synced_lyrics("A", "Song", "Album", 200)
    assert len(calls) == 1  # second identical lookup served from the per-track cache


def test_definitive_miss_is_cached(monkeypatch):
    # A real "no lyrics" (404 on both endpoints) IS memoized — no repeat network hit.
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp(404, {"code": 404})

    monkeypatch.setattr(lyrics.httpx, "get", fake_get)
    assert lyrics.fetch_synced_lyrics("A", "Song", "Album", 200) is None
    assert lyrics.fetch_synced_lyrics("A", "Song", "Album", 200) is None
    # First lookup: exact get + canonical get + search (all 404); second fully cached.
    assert len(calls) == 3


@pytest.mark.parametrize("failure", ["raise", 429, 503])
def test_transient_failure_is_not_cached(monkeypatch, failure):
    # A blip (network error / rate-limit / 5xx) must NOT be memoized as a miss:
    # once the service recovers, the very same lookup succeeds (proves a retry happened).
    calls = []
    state = {"down": True}

    def fake_get(url, **kw):
        calls.append(url)
        if state["down"]:
            if failure == "raise":
                raise RuntimeError("network down")
            return _Resp(failure, {})
        return _Resp(200, {"syncedLyrics": _SYNCED})

    monkeypatch.setattr(lyrics.httpx, "get", fake_get)
    assert lyrics.fetch_synced_lyrics("A", "Song", "Album", 200) is None  # blip → None, uncached
    state["down"] = False
    assert lyrics.fetch_synced_lyrics("A", "Song", "Album", 200) == _SYNCED  # recovered → retried
    assert len(calls) >= 2


# --- write_lrc_for round-trip -------------------------------------------------
# Needs a REAL audio file: write_lrc_for reads final tags via mutagen(easy=True),
# which requires actual MPEG frames — so build one with ffmpeg (a hard runtime dep),
# skipped when ffmpeg is absent, exactly like the M4A/Opus tests in test_fix_music_tags.
_FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not on PATH")


def _ffmpeg_mp3(path, *, title, artist, album) -> None:
    subprocess.run(
        [_FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.2",
         "-metadata", f"title={title}", "-metadata", f"artist={artist}",
         "-metadata", f"album={album}", "-codec:a", "libmp3lame", str(path)],
        check=True, capture_output=True,
    )


@needs_ffmpeg
def test_write_lrc_for_writes_sidecar_with_primary_artist(tmp_path, monkeypatch):
    p = tmp_path / "0001 - Song.mp3"
    _ffmpeg_mp3(p, title="Song", artist="A / B", album="X")
    seen = {}

    def fake_fetch(artist, title, album=None, duration=None):
        seen.update(artist=artist, title=title, album=album)
        return _SYNCED

    monkeypatch.setattr(lyrics, "fetch_synced_lyrics", fake_fetch)
    assert lyrics.write_lrc_for(p) is True
    assert p.with_suffix(".lrc").read_text(encoding="utf-8") == _SYNCED
    assert seen["artist"] == "A"   # primary only — the `/ B` feat segment is dropped
    assert seen["title"] == "Song"


@needs_ffmpeg
def test_write_lrc_for_no_match_writes_nothing(tmp_path, monkeypatch):
    p = tmp_path / "0002 - Miss.mp3"
    _ffmpeg_mp3(p, title="Miss", artist="A", album="X")
    monkeypatch.setattr(lyrics, "fetch_synced_lyrics", lambda *a, **k: None)
    assert lyrics.write_lrc_for(p) is False
    assert not p.with_suffix(".lrc").exists()  # a miss leaves no sidecar


def test_write_lrc_sidecars_empty_is_noop():
    seen = []
    assert lyrics.write_lrc_sidecars([], progress=lambda d, t: seen.append((d, t))) == 0
    assert seen == []  # no tracks → no progress ticks


@needs_ffmpeg
def test_write_lrc_sidecars_bulk_writes_all_and_reports_progress(tmp_path, monkeypatch):
    paths = []
    for i in range(3):
        p = tmp_path / f"{i:04d} - T{i}.mp3"
        _ffmpeg_mp3(p, title=f"T{i}", artist="A", album="X")
        paths.append(p)
    monkeypatch.setattr(lyrics, "fetch_synced_lyrics", lambda *a, **k: _SYNCED)

    seen = []
    written = lyrics.write_lrc_sidecars(paths, progress=lambda d, t: seen.append((d, t)))
    assert written == 3
    for p in paths:
        assert p.with_suffix(".lrc").read_text(encoding="utf-8") == _SYNCED
    assert seen[0] == (0, 3)      # emits 0/total up front
    assert seen[-1] == (3, 3)     # …and reaches total/total
    assert [d for d, _ in seen] == [0, 1, 2, 3]  # monotone completion count
