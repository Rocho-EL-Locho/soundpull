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
    # Once a path is known, a later DELIVERY of the same track does not overwrite it.
    with _mem_session() as session:
        record_tracks(session, 1, [("Drake", "Hotline Bling", "first/path.mp3")])
        session.commit()
        record_tracks(session, 1, [("Drake", "Hotline Bling", "second/path.mp3")])
        session.commit()
        assert library_index.load_index_paths(session, 1)[("drake", "hotline bling")] \
            == "first/path.mp3"


def test_record_tracks_update_path_refreshes_moved_file():
    # An authoritative scan (update_path=True) refreshes a moved/retagged file's path,
    # so the stored path stays valid and won't be pruned as missing (issue #31).
    with _mem_session() as session:
        record_tracks(session, 1, [("Drake", "One Dance", "Drake/Views/One Dance.mp3")])
        session.commit()
        record_tracks(session, 1, [("Drake", "One Dance", "Drake/More Life/One Dance.mp3")],
                      update_path=True)
        session.commit()
        assert library_index.load_index_paths(session, 1)[("drake", "one dance")] \
            == "Drake/More Life/One Dance.mp3"


def test_authoritative_scan_move_keeps_track():
    # End-to-end of the bug the update_path fix closes: a file moves on the server; an
    # authoritative rescan (record update_path=True, then prune) must KEEP the track under
    # its new path — not lose it. Regression for the record/prune interaction (issue #31).
    with _mem_session() as session:
        record_tracks(session, 1, [("Drake", "One Dance", "Drake/Views/One Dance.mp3")])
        session.commit()
        # Rescan finds only the NEW path (old one deleted): update, then prune to found set.
        record_tracks(session, 1, [("Drake", "One Dance", "Drake/More Life/One Dance.mp3")],
                      update_path=True)
        pruned = library_index._prune_missing(session, 1, {"Drake/More Life/One Dance.mp3"})
        session.commit()
        assert pruned == 0                                  # nothing lost
        assert is_on_server(session, 1, "Drake", "One Dance")


def test_artist_title_from_path_layouts():
    # <artist>/<album>/<title>.mp3 → artist + title
    assert library_index._artist_title_from_path(["Drake", "Views", "Hotline Bling.mp3"]) \
        == ("Drake", "Hotline Bling")
    # playlist folder <name>/NNNN - <title>.mp3 → title only (index prefix stripped)
    assert library_index._artist_title_from_path(["My Mix", "0007 - Some Song.mp3"]) \
        == ("", "Some Song")


def test_is_skippable_dir():
    # Decision is on the BASENAME only — independent of the parent path / library layout
    # (varied parents below). A cache dir is skipped at its top, so the walk never
    # descends to its "normal-named" children (cf/15/…) at all.
    assert library_index._is_skippable_dir("any/library/root/__sized__")  # "__" prefix
    assert library_index._is_skippable_dir("whatever/.trash")             # hidden
    assert library_index._is_skippable_dir("attachments")                 # even at the root
    assert library_index._is_skippable_dir("A/B/C/Attachments")           # case-insensitive
    assert not library_index._is_skippable_dir("root/__sized__/attachments/cf/15")  # basename "15"
    assert not library_index._is_skippable_dir("root/Drake")              # a real artist folder
    assert not library_index._is_skippable_dir("Drake/Views")             # a real album folder


def test_prune_missing_removes_vanished_pathful_keeps_present_and_pathless():
    # Authoritative scan (issue #31): a row whose file is no longer on the server is
    # pruned; a still-present file and a pathless (mark_existing) row are kept.
    with _mem_session() as session:
        record_tracks(session, 1, [
            ("Drake", "One Dance", "Drake/Views/One Dance.mp3"),   # still present
            ("Adele", "Hello", "Adele/25/Hello.mp3"),              # deleted from server
            ("Ghost", "Marked"),                                   # pathless seed
        ])
        session.commit()
        pruned = library_index._prune_missing(session, 1, {"Drake/Views/One Dance.mp3"})
        session.commit()
        assert pruned == 1
        paths = library_index.load_index_paths(session, 1)
        assert ("drake", "one dance") in paths      # present → kept
        assert ("adele", "hello") not in paths       # vanished → pruned
        assert ("ghost", "marked") in paths          # pathless → untouched


def test_prune_missing_is_scoped_per_user():
    with _mem_session() as session:
        record_tracks(session, 1, [("A", "T", "A/T.mp3")])
        record_tracks(session, 2, [("A", "T", "A/T.mp3")])
        session.commit()
        # User 1's scan found nothing → prunes only user 1's row, never user 2's.
        assert library_index._prune_missing(session, 1, set()) == 1
        session.commit()
        assert library_index.load_index_paths(session, 1) == {}
        assert ("a", "t") in library_index.load_index_paths(session, 2)


def test_walk_remote_files_records_listing_errors():
    # A failed directory listing is recorded so scan_webdav knows the walk was
    # incomplete and must NOT prune (issue #31).
    class FailingClient:
        def ls(self, path, detail=True):
            if path == "":
                return [{"name": "unreadable", "type": "directory"}]
            raise OSError("boom")

    errors: list = []
    files = list(library_index._walk_remote_files(FailingClient(), "", 0, 8, errors))
    assert files == []
    assert len(errors) == 1 and errors[0][0] == "unreadable"


def test_walk_remote_files_skips_cache_and_hidden_subtrees():
    # The scan must not descend into a server-side cache tree (e.g. "__sized__/…") or a
    # hidden dir — those hold no music and would cost thousands of PROPFINDs. A fake
    # client records which paths get listed.
    tree = {
        "": [{"name": "Drake", "type": "directory"},
             {"name": "__sized__", "type": "directory"},
             {"name": "attachments", "type": "directory"},
             {"name": ".trash", "type": "directory"}],
        "Drake": [{"name": "Drake/Views", "type": "directory"}],
        "Drake/Views": [{"name": "Drake/Views/Hotline Bling.mp3", "type": "file"}],
        "__sized__": [{"name": "__sized__/attachments", "type": "directory"}],
        "attachments": [{"name": "attachments/0d", "type": "directory"}],
        ".trash": [{"name": ".trash/old.mp3", "type": "file"}],
    }
    listed: list[str] = []

    class FakeClient:
        def ls(self, path, detail=True):
            listed.append(path)
            return tree.get(path, [])

    files = list(library_index._walk_remote_files(FakeClient(), "", depth=0, max_depth=8))
    assert files == ["Drake/Views/Hotline Bling.mp3"]   # only the real music file
    assert "__sized__" not in listed                    # cache subtree never listed
    assert "attachments" not in listed                  # attachments store never listed
    assert ".trash" not in listed                       # hidden subtree never listed
