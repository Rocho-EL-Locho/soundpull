"""Tests for the interactive history-page helpers (issue #44).

Logic-level only (no NiceGUI render): the query builder and the retry-options
derivation are pure functions, exercised against an in-memory SQLite DB — the same
style as tests/test_library_index.py.
"""
from datetime import datetime, timezone

from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401 - register tables on SQLModel.metadata before create_all
from app.models import DownloadHistory, UserSettings
from app.pages.history import build_history_query, retry_options


def _session() -> Session:
    engine = create_engine("sqlite://")  # in-memory
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _add(session: Session, **kw) -> DownloadHistory:
    defaults = dict(user_id=1, genre="Pop", mode="album", destination_type="browser",
                    audio_format="mp3_320", phase="done")
    defaults.update(kw)
    row = DownloadHistory(**defaults)
    session.add(row)
    session.commit()
    return row


def _seed(session: Session) -> None:
    _add(session, id="a", url="https://ex/1", artist="Drake", album="Views", mode="album",
         destination_type="webdav", phase="done",
         created_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    _add(session, id="b", url="https://ex/2", artist="Adele", album="25", mode="single",
         destination_type="browser", phase="error",
         created_at=datetime(2026, 7, 5, tzinfo=timezone.utc))
    _add(session, id="c", url="https://ex/3", artist="Drake", album="Scorpion", mode="artist",
         destination_type="webdav", phase="download",
         created_at=datetime(2026, 7, 9, tzinfo=timezone.utc))


def _ids(session: Session, **filters) -> list[str]:
    return [r.id for r in session.exec(build_history_query(1, **filters)).all()]


# --- build_history_query ---------------------------------------------------

def test_no_filter_returns_all_newest_first():
    session = _session()
    _seed(session)
    assert _ids(session) == ["c", "b", "a"]


def test_query_scoped_to_user():
    session = _session()
    _seed(session)
    _add(session, id="other", user_id=2, url="https://ex/9", artist="Drake")
    assert set(_ids(session)) == {"a", "b", "c"}  # user 2's row excluded


def test_search_matches_artist_case_insensitive():
    session = _session()
    _seed(session)
    assert set(_ids(session, search="drake")) == {"a", "c"}


def test_search_matches_album_and_url():
    session = _session()
    _seed(session)
    assert _ids(session, search="scorpion") == ["c"]
    assert _ids(session, search="ex/2") == ["b"]


def test_filter_mode_and_dest():
    session = _session()
    _seed(session)
    assert _ids(session, mode="artist") == ["c"]
    assert set(_ids(session, dest="webdav")) == {"a", "c"}


def test_filter_status_running_groups_phases():
    session = _session()
    _seed(session)
    assert _ids(session, status="running") == ["c"]      # phase "download"
    assert _ids(session, status="done") == ["a"]
    assert _ids(session, status="error") == ["b"]


def test_date_range_inclusive_of_whole_to_day():
    session = _session()
    _seed(session)
    assert _ids(session, date_from="2026-07-05", date_to="2026-07-05") == ["b"]
    assert _ids(session, date_from="2026-07-05") == ["c", "b"]
    assert _ids(session, date_to="2026-07-05") == ["b", "a"]


def test_malformed_date_is_ignored():
    session = _session()
    _seed(session)
    assert _ids(session, date_from="not-a-date") == ["c", "b", "a"]


# --- retry_options ---------------------------------------------------------

def test_retry_options_mirrors_stored_fields():
    row = DownloadHistory(id="x", user_id=1, url="https://ex/1", genre="Rap",
                          mode="album", audio_format="opus", destination_type="browser")
    opts = retry_options(row, None)
    assert opts["url"] == "https://ex/1"
    assert opts["genre"] == "Rap"
    assert opts["mode"] == "album"
    assert opts["audio_format"] == "opus"
    assert opts["destination_type"] == "browser"
    assert opts["tag_options"] is None  # → start_job fills from current settings


def test_retry_options_dedup_and_lyrics_from_settings_webdav():
    row = DownloadHistory(id="x", user_id=1, url="u", genre="Pop", mode="album",
                          destination_type="webdav")
    us = UserSettings(user_id=1, default_genre="Pop", default_mode="album",
                      destination_type="webdav", dedup_skip_existing=True,
                      fetch_synced_lyrics=True)
    opts = retry_options(row, us)
    assert opts["dedup"] is True
    assert opts["fetch_lyrics"] is True


def test_retry_options_dedup_off_for_browser_even_if_setting_on():
    row = DownloadHistory(id="x", user_id=1, url="u", genre="Pop", mode="album",
                          destination_type="browser")
    us = UserSettings(user_id=1, default_genre="Pop", default_mode="album",
                      destination_type="browser", dedup_skip_existing=True)
    assert retry_options(row, us)["dedup"] is False  # dedup is WebDAV-only


def test_retry_options_artist_forces_dedup():
    row = DownloadHistory(id="x", user_id=1, url="u", genre="Pop", mode="artist",
                          destination_type="webdav")
    us = UserSettings(user_id=1, default_genre="Pop", default_mode="album",
                      destination_type="webdav", dedup_skip_existing=False)
    assert retry_options(row, us)["dedup"] is True  # artist runs auto-dedup


def test_retry_options_no_settings_defaults_off():
    row = DownloadHistory(id="x", user_id=1, url="u", genre="Pop", mode="album",
                          destination_type="browser")
    opts = retry_options(row, None)
    assert opts["dedup"] is False
    assert opts["fetch_lyrics"] is False
