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
