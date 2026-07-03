"""Job worker helpers (issue #21)."""
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from app import jobs
from app.jobs import _clean_error, _record_delivered_safe


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))


def test_clean_error_strips_ansi_colour_codes():
    # yt-dlp colourises errors; the stored/displayed message must be clean text.
    colored = "\x1b[0;31mERROR:\x1b[0m [youtube] kFl4bPPLlhg: Video unavailable"
    assert _clean_error(Exception(colored)) == "ERROR: [youtube] kFl4bPPLlhg: Video unavailable"


def test_clean_error_plain_text_unchanged():
    assert _clean_error(ValueError("kein WebDAV-Ziel")) == "kein WebDAV-Ziel"


@contextmanager
def _dummy_scope():
    yield object()


def test_record_delivered_safe_returns_true_on_success(monkeypatch):
    # A clean index write reports success so the caller leaves the job free of warnings (#38).
    monkeypatch.setattr(jobs, "session_scope", _dummy_scope)
    monkeypatch.setattr(jobs.library_index, "record_tracks", lambda *a, **k: 1)
    assert _record_delivered_safe("job", 1, [("Drake", "One Dance")]) is True


def test_record_delivered_safe_returns_false_on_db_error(monkeypatch):
    # A swallowed DB error must be reported as False (never raised) so the completed upload
    # stays "done" but the caller can surface the stale-index warning (issue #38).
    monkeypatch.setattr(jobs, "session_scope", _dummy_scope)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(jobs.library_index, "record_tracks", boom)
    assert _record_delivered_safe("job", 1, [("Drake", "One Dance")]) is False


def test_record_delivered_safe_retries_once_on_integrity_race(monkeypatch):
    # A benign unique-constraint race rolls back the batch; the retry re-records the
    # remainder and reports success — so no false "index update failed" warning (issue #38).
    monkeypatch.setattr(jobs, "session_scope", _dummy_scope)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _integrity_error()
        return 0

    monkeypatch.setattr(jobs.library_index, "record_tracks", flaky)
    assert _record_delivered_safe("job", 1, [("Drake", "One Dance")]) is True
    assert calls["n"] == 2  # retried exactly once


def test_record_delivered_safe_false_on_persistent_conflict(monkeypatch):
    # A conflict that survives the retry is a genuine failure → False (surfaces a warning).
    monkeypatch.setattr(jobs, "session_scope", _dummy_scope)

    def always_conflict(*a, **k):
        raise _integrity_error()

    monkeypatch.setattr(jobs.library_index, "record_tracks", always_conflict)
    assert _record_delivered_safe("job", 1, [("Drake", "One Dance")]) is False


def _arm_artist_job(monkeypatch, dest_type):
    """Register an artist JobState and stub the DB/index side-effects for _run_artist."""
    from app.fix_music_tags import TagOptions

    js = jobs.JobState(id="art1", user_id=7, url="u", genre="Rap", mode="artist",
                       destination_type=dest_type, tag_options=TagOptions())
    jobs._registry["art1"] = js
    monkeypatch.setattr(jobs, "session_scope", _dummy_scope)
    monkeypatch.setattr(jobs, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(jobs.library_index, "load_index_paths",
                        lambda session, uid: {jobs.library_index.track_key("Song", "Artist"): "p"})
    return js


def test_run_artist_auto_dedups_on_webdav(monkeypatch):
    # Artist runs on WebDAV ALWAYS build the on_server closure (no per-download toggle), and the
    # album pool is clamped to the 1–4 range regardless of the env value.
    from app.fix_music_tags import TagOptions
    from app.pipeline import Result

    _arm_artist_job(monkeypatch, "webdav")
    monkeypatch.setattr(jobs.settings, "max_artist_album_concurrency", 9)  # over the cap
    captured = {}
    monkeypatch.setattr(jobs, "run_artist_download",
                        lambda **kw: captured.update(kw) or Result(summary="ok"))

    jobs._run_artist("art1", "u", "Rap", jobs.Destination(type="webdav"),
                     "mp3_320", TagOptions(), None)

    on_server = captured["on_server"]
    assert on_server is not None
    assert on_server("Artist", "Song") is True          # present in the loaded index → skip
    assert on_server("Nobody", "Nothing") is False
    assert captured["album_concurrency"] == 4            # 9 clamped down to the max


def test_run_artist_no_dedup_for_browser(monkeypatch):
    # A browser ZIP has no library to dedup against → on_server stays None (full download).
    from app.fix_music_tags import TagOptions
    from app.pipeline import Result

    _arm_artist_job(monkeypatch, "browser")
    captured = {}
    monkeypatch.setattr(jobs, "run_artist_download",
                        lambda **kw: captured.update(kw) or Result(summary="ok"))

    jobs._run_artist("art1", "u", "Rap", jobs.Destination(type="browser"),
                     "mp3_320", TagOptions(), None)

    assert captured["on_server"] is None
