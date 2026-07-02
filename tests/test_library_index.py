"""Server-content index (issue #21): key normalisation + record/lookup round-trip.

The one invariant that must hold: the key computed from RAW yt-dlp metadata (at
match-filter time) equals the key computed from the FINAL tags (when recording a
delivered track) — otherwise a synced track would never be recognised as "on the
server" and would re-download every run.
"""
import app.models  # noqa: F401 — registers tables on SQLModel.metadata
from sqlmodel import Session, SQLModel, create_engine

from app import library_index
from app.library_index import is_on_server, load_index, record_tracks, track_key


def _mem_session() -> Session:
    engine = create_engine("sqlite://")  # in-memory
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_track_key_matches_raw_and_tagged_forms():
    # RAW yt-dlp: comma-separated artists, "(feat. …)" in the title.
    raw = track_key("Song (feat. Guest)", "Primary, Guest")
    # FINAL tags: " / "-separated artist, feat stripped from the title.
    tagged = track_key("Song", "Primary / Guest")
    assert raw == tagged == ("primary", "song")


def test_track_key_is_case_and_whitespace_insensitive():
    assert track_key("  Hello   World ", "Drake") == track_key("hello world", "drake")


def test_track_key_handles_bracket_feat_and_ft():
    assert track_key("Tune [ft. X]", "A")[1] == "tune"
    assert track_key("Tune ft. X", "A")[1] == "tune"


def test_record_and_lookup_round_trip():
    with _mem_session() as session:
        added = record_tracks(session, user_id=1, pairs=[("Drake", "Hotline Bling"),
                                                         ("Adele", "Hello")])
        session.commit()
        assert added == 2
        # Same track in raw feat form is recognised as already present.
        assert is_on_server(session, 1, "Drake", "Hotline Bling")
        assert is_on_server(session, 1, "Adele", "Hello (feat. Nobody)")
        # A different user's library is isolated.
        assert not is_on_server(session, 2, "Drake", "Hotline Bling")
        # Unknown track is not on the server.
        assert not is_on_server(session, 1, "Drake", "God's Plan")


def test_record_tracks_dedupes():
    with _mem_session() as session:
        record_tracks(session, 1, [("Drake", "Hotline Bling")])
        session.commit()
        # Re-recording the same track (even in a different textual form) adds nothing.
        again = record_tracks(session, 1, [("Drake", "Hotline Bling (feat. X)"),
                                           ("Drake", "hotline bling")])
        session.commit()
        assert again == 0
        assert load_index(session, 1) == {("drake", "hotline bling")}


def test_record_tracks_skips_titleless():
    with _mem_session() as session:
        assert record_tracks(session, 1, [("Drake", "")]) == 0


def test_record_tracks_stores_and_loads_rel_path():
    # Dedup (issue #31): a delivered track's library-relative path is stored and returned
    # by load_index_paths so a later playlist can reference the existing file.
    with _mem_session() as session:
        added = record_tracks(session, 1, [
            ("Drake", "Hotline Bling", "Drake/Views/Hotline Bling.mp3")])
        session.commit()
        assert added == 1
        paths = library_index.load_index_paths(session, 1)
        assert paths == {("drake", "hotline bling"): "Drake/Views/Hotline Bling.mp3"}


def test_record_tracks_accepts_mixed_2_and_3_tuples():
    # 2-tuple callers (mark_existing seed) keep working alongside 3-tuple (path) callers.
    with _mem_session() as session:
        record_tracks(session, 1, [("Adele", "Hello"),
                                   ("Drake", "One Dance", "Drake/Views/One Dance.mp3")])
        session.commit()
        paths = library_index.load_index_paths(session, 1)
        assert paths[("adele", "hello")] is None            # no path known
        assert paths[("drake", "one dance")] == "Drake/Views/One Dance.mp3"


def test_record_tracks_backfills_null_path():
    # A track first seen without a path (mark_existing) gets its path backfilled when it
    # is later actually delivered — even in raw feat form (issue #31).
    with _mem_session() as session:
        record_tracks(session, 1, [("Drake", "Hotline Bling")])
        session.commit()
        added = record_tracks(session, 1, [
            ("Drake", "Hotline Bling (feat. X)", "Drake/Views/Hotline Bling.mp3")])
        session.commit()
        assert added == 0                                   # backfill, not a new row
        paths = library_index.load_index_paths(session, 1)
        assert paths[("drake", "hotline bling")] == "Drake/Views/Hotline Bling.mp3"


def test_record_tracks_keeps_first_known_path():
    # Once a path is known, a later delivery of the same track does not overwrite it.
    with _mem_session() as session:
        record_tracks(session, 1, [("Drake", "Hotline Bling", "first/path.mp3")])
        session.commit()
        record_tracks(session, 1, [("Drake", "Hotline Bling", "second/path.mp3")])
        session.commit()
        assert library_index.load_index_paths(session, 1)[("drake", "hotline bling")] \
            == "first/path.mp3"


def test_artist_title_from_path_layouts():
    # <artist>/<album>/<title>.mp3 → artist + title
    assert library_index._artist_title_from_path(["Drake", "Views", "Hotline Bling.mp3"]) \
        == ("Drake", "Hotline Bling")
    # playlist folder <name>/NNNN - <title>.mp3 → title only (index prefix stripped)
    assert library_index._artist_title_from_path(["My Mix", "0007 - Some Song.mp3"]) \
        == ("", "Some Song")
