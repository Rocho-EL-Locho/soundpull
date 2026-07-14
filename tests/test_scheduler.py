"""Scheduler due-calculation (issue #21) — pure, no threads/DB."""
from datetime import datetime, timedelta, timezone

from app.models import PlaylistSubscription, UserSettings
from app.scheduler import _is_due, _library_scan_due

_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _sub(**kw) -> PlaylistSubscription:
    base = dict(user_id=1, url="u", interval_hours=24, enabled=True, last_checked_at=None)
    base.update(kw)
    return PlaylistSubscription(**base)


def test_never_checked_is_due():
    assert _is_due(_sub(last_checked_at=None), _NOW) is True


def test_disabled_is_never_due():
    assert _is_due(_sub(enabled=False, last_checked_at=None), _NOW) is False


def test_within_interval_not_due():
    recent = _NOW - timedelta(hours=1)
    assert _is_due(_sub(interval_hours=24, last_checked_at=recent), _NOW) is False


def test_past_interval_is_due():
    old = _NOW - timedelta(hours=25)
    assert _is_due(_sub(interval_hours=24, last_checked_at=old), _NOW) is True


def test_naive_last_checked_treated_as_utc():
    old_naive = (_NOW - timedelta(hours=25)).replace(tzinfo=None)
    assert _is_due(_sub(interval_hours=24, last_checked_at=old_naive), _NOW) is True


# --- scheduled library scan (roadmap 03) -----------------------------------

def _us(**kw) -> UserSettings:
    base = dict(user_id=1, webdav_url="http://dav.example", library_scan_interval_hours=24,
                last_library_scan_at=None)
    base.update(kw)
    return UserSettings(**base)


def test_scan_interval_zero_is_off():
    assert _library_scan_due(_us(library_scan_interval_hours=0), _NOW) is False


def test_scan_without_webdav_is_off():
    assert _library_scan_due(_us(webdav_url=None), _NOW) is False


def test_scan_never_run_is_due():
    assert _library_scan_due(_us(last_library_scan_at=None), _NOW) is True


def test_scan_within_interval_not_due():
    recent = _NOW - timedelta(hours=1)
    assert _library_scan_due(_us(last_library_scan_at=recent), _NOW) is False


def test_scan_past_interval_is_due():
    old = _NOW - timedelta(hours=25)
    assert _library_scan_due(_us(last_library_scan_at=old), _NOW) is True


def test_scan_naive_last_scan_treated_as_utc():
    old_naive = (_NOW - timedelta(hours=25)).replace(tzinfo=None)
    assert _library_scan_due(_us(last_library_scan_at=old_naive), _NOW) is True
