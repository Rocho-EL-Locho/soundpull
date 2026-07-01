"""Additive auto-migration: `reconcile_columns` heals schema drift.

Regression for the "no such column: usersettings.language" crash — an old on-disk
DB (created before new model columns were added) must gain the missing columns on
startup instead of crashing every query.
"""
import app.models  # noqa: F401  — registers tables on SQLModel.metadata
from sqlalchemy import create_engine, inspect, text

from app.db import reconcile_columns

# A `usersettings` table as it looked before default_audio_format / language /
# the tag_* fields existed. One row stands in for real production data.
_OLD_SCHEMA = """
CREATE TABLE usersettings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    default_genre VARCHAR NOT NULL,
    default_mode VARCHAR NOT NULL,
    destination_type VARCHAR NOT NULL,
    updated_at DATETIME NOT NULL
)
"""
_OLD_ROW = (
    "INSERT INTO usersettings "
    "(id, user_id, default_genre, default_mode, destination_type, updated_at) "
    "VALUES (1, 1, 'Pop', 'album', 'browser', '2020-01-01 00:00:00')"
)


def _old_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text(_OLD_SCHEMA))
        conn.execute(text(_OLD_ROW))
    return engine


def test_reconcile_adds_missing_columns(tmp_path):
    engine = _old_db(tmp_path)

    reconcile_columns(engine)

    cols = {c["name"] for c in inspect(engine).get_columns("usersettings")}
    assert {"default_audio_format", "language", "tag_genre", "tag_album_artist",
            "tag_cover", "tag_track_number", "tag_feat_artist", "tag_comments",
            "webdav_url"} <= cols

    # The pre-existing row must be readable and carry the model defaults.
    with engine.connect() as conn:
        lang, fmt, tag_genre, webdav = conn.execute(text(
            "SELECT language, default_audio_format, tag_genre, webdav_url "
            "FROM usersettings WHERE id = 1"
        )).one()
    assert lang == "de"              # NOT NULL default backfilled onto old row
    assert fmt == "mp3_320"
    assert tag_genre in (1, True)    # bool default True
    assert webdav is None            # nullable, no default


def test_reconcile_is_idempotent(tmp_path):
    engine = _old_db(tmp_path)
    reconcile_columns(engine)
    before = {c["name"] for c in inspect(engine).get_columns("usersettings")}
    reconcile_columns(engine)  # second run must be a harmless no-op
    after = {c["name"] for c in inspect(engine).get_columns("usersettings")}
    assert before == after
