"""Server-content index (issue #21): key normalisation + record/lookup round-trip.

The one invariant that must hold: the key computed from RAW yt-dlp metadata (at
match-filter time) equals the key computed from the FINAL tags (when recording a
delivered track) — otherwise a synced track would never be recognised as "on the
server" and would re-download every run.
"""
from contextlib import contextmanager

import app.models  # noqa: F401 — registers tables on SQLModel.metadata
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import library_index
from app.library_index import is_on_server, load_index, record_tracks, track_key
from app.models import UserSettings


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


def test_scan_skips_soundpull_trash_dir():
    # Roadmap 01 acceptance criterion 1: a trashed file lives under `.soundpull-trash/…`,
    # which the scan already skips (leading-dot rule) — so it's never re-indexed. The dir
    # is also listed explicitly in _SKIP_DIR_NAMES for self-documentation.
    from app.library_ops import TRASH_DIR
    assert library_index._is_skippable_dir(TRASH_DIR)
    assert library_index._is_skippable_dir(f"lib/{TRASH_DIR}")
    assert TRASH_DIR in library_index._SKIP_DIR_NAMES


def test_remove_by_rel_path_and_update_rel_path():
    # Roadmap 01: the ops layer keeps the index in sync path-based.
    with _mem_session() as session:
        record_tracks(session, 1, [("Drake", "One Dance", "Drake/Views/One Dance.mp3")])
        session.commit()
        # Repoint to a moved location.
        assert library_index.update_rel_path(
            session, 1, "Drake/Views/One Dance.mp3", "Drake/Best Of/One Dance.mp3") == 1
        session.commit()
        assert library_index.load_index_paths(session, 1)[("drake", "one dance")] \
            == "Drake/Best Of/One Dance.mp3"
        # Remove by the new path.
        assert library_index.remove_by_rel_path(session, 1, "Drake/Best Of/One Dance.mp3") == 1
        session.commit()
        assert library_index.load_index(session, 1) == set()
        # Removing an unknown path is a no-op.
        assert library_index.remove_by_rel_path(session, 1, "nope.mp3") == 0


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


def test_walk_remote_files_raises_when_root_unreadable():
    # A failure listing the ROOT means the target is unreachable/misconfigured — it must
    # PROPAGATE so scan_webdav fails loudly instead of returning a silent empty no-op that
    # looks like a healthy but empty library (issue #38).
    class DeadClient:
        def ls(self, path, detail=True):
            raise OSError("connection refused")

    errors: list = []
    with pytest.raises(OSError):
        list(library_index._walk_remote_files(DeadClient(), "", 0, 8, errors))
    assert errors == []  # a root failure is raised, not recorded-and-swallowed


def _scan_env(monkeypatch, walk):
    """Wire scan_webdav's collaborators to an in-memory DB + a fake walk (issue #38).

    Returns the engine so a test can pre-seed / inspect index rows. `walk` replaces
    `_walk_remote_files`; `make_client` is stubbed (the fake walk ignores the client) and a
    single-connection in-memory engine backs `session_scope`.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(UserSettings(user_id=1, webdav_url="http://dav.example", webdav_folder=""))
        s.commit()

    @contextmanager
    def fake_scope():
        session = Session(engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr("app.db.session_scope", fake_scope)
    monkeypatch.setattr("app.webdav_util.make_client", lambda *a, **k: object())
    monkeypatch.setattr(library_index, "_walk_remote_files", walk)
    return engine


def test_scan_webdav_reports_errors_and_skips_prune(monkeypatch):
    # An incomplete walk (a sub-folder listing failed) must return the errors AND skip
    # pruning, so a transient failure can't wipe still-present index rows (issue #38).
    def walk(client, base, depth, max_depth, errors=None):
        if errors is not None:
            errors.append(("Artist/Bad", "boom"))
        return iter(())

    engine = _scan_env(monkeypatch, walk)
    with Session(engine) as s:  # a stale row a *clean* scan would prune
        record_tracks(s, 1, [("Ghost", "Gone", "Ghost/Album/Gone.mp3")])
        s.commit()

    added, pruned, errors = library_index.scan_webdav(1)

    assert added == 0
    assert pruned == 0                              # incomplete walk → never prunes
    assert errors and errors[0][0] == "Artist/Bad"  # surfaced to the caller
    with Session(engine) as s:
        assert ("ghost", "gone") in library_index.load_index_paths(s, 1)  # row kept


def test_scan_webdav_clean_walk_reports_no_errors(monkeypatch):
    # A successful walk returns an empty errors list — distinct from the incomplete case so
    # the UI can tell "nothing to skip" apart from "index unavailable" (issue #38).
    def walk(client, base, depth, max_depth, errors=None):
        yield "Artist/Album/Song.mp3"

    library_index_engine = _scan_env(monkeypatch, walk)

    added, pruned, errors = library_index.scan_webdav(1)

    assert added == 1
    assert errors == []
    with Session(library_index_engine) as s:
        assert ("artist", "song") in library_index.load_index_paths(s, 1)


# --- Lyrics backfill (LRCGET-style, issue #43) --------------------------------

def test_walk_audio_with_lrc_flags_existing_sidecars():
    # Per audio file, report whether a sibling `.lrc` already exists (from the same
    # listing); non-audio files are ignored and cache dirs are skipped.
    tree = {
        "": [{"name": "Artist", "type": "directory"},
             {"name": "cache", "type": "directory"}],
        "Artist": [{"name": "Artist/Album", "type": "directory"}],
        "Artist/Album": [
            {"name": "Artist/Album/01 - A.mp3", "type": "file"},
            {"name": "Artist/Album/02 - B.mp3", "type": "file"},
            {"name": "Artist/Album/02 - B.lrc", "type": "file"},
            {"name": "Artist/Album/cover.jpg", "type": "file"},
        ],
        "cache": [{"name": "cache/x.mp3", "type": "file"}],
    }

    class Fake:
        def ls(self, path, detail=True):
            return tree.get(path, [])

    out = dict(library_index._walk_audio_with_lrc(Fake(), "", 0, 8))
    assert out == {"Artist/Album/01 - A.mp3": False,   # needs a sidecar
                   "Artist/Album/02 - B.mp3": True}    # already has one; cover.jpg + cache/ ignored


def test_backfill_lyrics_writes_missing_and_skips_existing(monkeypatch):
    tree = {
        "": [{"name": "Artist", "type": "directory"}],
        "Artist": [{"name": "Artist/Album", "type": "directory"}],
        "Artist/Album": [
            {"name": "Artist/Album/01 - Have.mp3", "type": "file"},
            {"name": "Artist/Album/01 - Have.lrc", "type": "file"},   # already covered
            {"name": "Artist/Album/02 - Get.mp3", "type": "file"},    # LRCLIB has it
            {"name": "Artist/Album/03 - None.mp3", "type": "file"},   # LRCLIB has nothing
        ],
    }
    uploaded: dict[str, bytes] = {}

    class FakeDav:
        def ls(self, path, detail=True):
            return tree.get(path, [])

        def upload_fileobj(self, fileobj, to_path, overwrite=False, **kw):
            uploaded[to_path] = fileobj.read()

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(UserSettings(user_id=1, webdav_url="http://dav.example", webdav_folder=""))
        s.commit()

    @contextmanager
    def fake_scope():
        session = Session(engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr("app.db.session_scope", fake_scope)
    monkeypatch.setattr("app.webdav_util.make_client", lambda *a, **k: FakeDav())
    monkeypatch.setattr("app.lyrics.fetch_synced_lyrics",
                        lambda artist, title, album=None, duration=None:
                        "[00:00.00]la" if title == "Get" else None)

    written, skipped, missing, errors = library_index.backfill_lyrics(1)

    assert (written, skipped, missing) == (1, 1, 1)   # Get written, Have skipped, None missing
    assert errors == []
    assert list(uploaded) == ["Artist/Album/02 - Get.lrc"]
    assert uploaded["Artist/Album/02 - Get.lrc"].decode() == "[00:00.00]la"


def _dav_env(monkeypatch, dav, folder: str = ""):
    """In-memory DB + a WebDAV-configured user, with `dav` as the make_client result."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(UserSettings(user_id=1, webdav_url="http://dav.example", webdav_folder=folder))
        s.commit()

    @contextmanager
    def fake_scope():
        session = Session(engine)
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr("app.db.session_scope", fake_scope)
    monkeypatch.setattr("app.webdav_util.make_client", lambda *a, **k: dav)
    return engine


def test_backfill_lyrics_prefix_scopes_to_folder(monkeypatch):
    # roadmap 03: an album-scoped backfill only touches files under the given prefix.
    tree = {
        "": [{"name": "A", "type": "directory"}, {"name": "B", "type": "directory"}],
        "A": [{"name": "A/Alb", "type": "directory"}],
        "A/Alb": [{"name": "A/Alb/01 - X.mp3", "type": "file"}],
        "B": [{"name": "B/Alb", "type": "directory"}],
        "B/Alb": [{"name": "B/Alb/01 - Y.mp3", "type": "file"}],
    }
    uploaded: dict[str, bytes] = {}

    class FakeDav:
        def ls(self, path, detail=True):
            return tree.get(path, [])

        def upload_fileobj(self, fileobj, to_path, overwrite=False, **kw):
            uploaded[to_path] = fileobj.read()

    _dav_env(monkeypatch, FakeDav())
    monkeypatch.setattr("app.lyrics.fetch_synced_lyrics",
                        lambda artist, title, album=None, duration=None: "[00:00.00]la")

    written, _skipped, _missing, errors = library_index.backfill_lyrics(1, prefix="A/Alb")

    assert errors == []
    assert written == 1
    assert list(uploaded) == ["A/Alb/01 - X.lrc"]   # B/Alb left untouched


def test_scan_webdav_stamps_last_scan(monkeypatch):
    # roadmap 03: a successful scan records `last_library_scan_at` for the "scanned Nh ago"
    # display and the scheduled-scan due-check.
    def walk(client, base, depth, max_depth, errors=None):
        yield "Artist/Album/Song.mp3"

    engine = _scan_env(monkeypatch, walk)
    library_index.scan_webdav(1)
    with Session(engine) as s:
        us = s.exec(select(UserSettings).where(UserSettings.user_id == 1)).first()
        assert us.last_library_scan_at is not None


# --- Library browser: grouping + search (roadmap 03) --------------------------

def test_is_playlist_folder():
    assert library_index.is_playlist_folder("DnB Mix [PLabcdefghij]")
    assert library_index.is_playlist_folder("Chill [OLAK5uy_abcdefghij]")
    assert not library_index.is_playlist_folder("Best of BCee [Deluxe]")  # short → an album
    assert not library_index.is_playlist_folder("BCee")


def test_split_rel_path():
    assert library_index.split_rel_path("BCee/Northpoint/01 - Intro.mp3") == \
        ("BCee", "Northpoint", "01 - Intro.mp3")
    # A single containing folder → no album level.
    assert library_index.split_rel_path("PL [id]/0001 - T.mp3")[1] == "—"
    assert library_index.split_rel_path("loose.mp3") == ("—", "—", "loose.mp3")


def _seed_tree(session) -> None:
    library_index.record_tracks(session, 1, [
        ("BCee", "Intro", "BCee/Northpoint/01 - Intro.mp3"),
        ("BCee", "Outro", "BCee/Northpoint/02 - Outro.mp3"),
        ("BCee", "Solo", "BCee/Best Of/01 - Solo.mp3"),
        ("A.M.C", "Fast", "A.M.C/Album/01 - Fast.mp3"),
        ("x", "PlTrack", "DnB Mix [PLabcdefghij]/0001 - PlTrack.mp3"),
    ])
    session.commit()


def test_library_tree_groups_artists_albums_playlists():
    with _mem_session() as s:
        _seed_tree(s)
        tree = library_index.library_tree(s, 1)
        assert [a.name for a in tree.artists] == ["A.M.C", "BCee"]   # sorted, case-insensitive
        bcee = next(a for a in tree.artists if a.name == "BCee")
        assert bcee.track_count == 3
        assert sorted(al.name for al in bcee.albums) == ["Best Of", "Northpoint"]
        assert [p.name for p in tree.playlists] == ["DnB Mix [PLabcdefghij]"]
        assert (tree.total_artists, tree.total_albums, tree.total_tracks) == (2, 4, 5)
        # Display titles come from the filename (index prefix stripped), not the stored key.
        northpoint = next(al for al in bcee.albums if al.name == "Northpoint")
        assert sorted(t.title for t in northpoint.tracks) == ["Intro", "Outro"]


def test_library_tree_skips_rows_without_path():
    with _mem_session() as s:
        library_index.record_tracks(s, 1, [("Ghost", "NoPath")])  # 2-tuple → rel_path None
        s.commit()
        assert library_index.library_tree(s, 1).total_tracks == 0


def test_count_stats():
    with _mem_session() as s:
        _seed_tree(s)
        assert library_index.count_stats(s, 1) == {"artists": 2, "albums": 4, "tracks": 5}


def test_filter_tree_matches_artist_album_and_title():
    with _mem_session() as s:
        _seed_tree(s)
        tree = library_index.library_tree(s, 1)
        # by artist name → whole artist kept
        assert [a.name for a in library_index.filter_tree(tree, "bcee").artists] == ["BCee"]
        # by album name → only the matching album
        f = library_index.filter_tree(tree, "northpoint")
        assert [al.name for a in f.artists for al in a.albums] == ["Northpoint"]
        # by track title → only matching tracks survive
        f2 = library_index.filter_tree(tree, "intro")
        assert [t.title for a in f2.artists for al in a.albums for t in al.tracks] == ["Intro"]
        # playlist match
        assert [p.name for p in library_index.filter_tree(tree, "dnb").playlists] == \
            ["DnB Mix [PLabcdefghij]"]
        # empty query → unchanged
        assert library_index.filter_tree(tree, "") is tree


def test_remove_by_prefix_drops_only_folder_rows():
    with _mem_session() as s:
        _seed_tree(s)
        removed = library_index.remove_by_prefix(s, 1, "BCee/Northpoint")
        s.commit()
        assert removed == 2
        paths = set(library_index.load_index_paths(s, 1).values())
        assert "BCee/Northpoint/01 - Intro.mp3" not in paths
        assert "BCee/Best Of/01 - Solo.mp3" in paths          # sibling album untouched


def test_folder_has_nested_tracks():
    with _mem_session() as s:
        record_tracks(s, 1, [
            ("BCee", "Solo", "BCee/Best Of/01 - Solo.mp3"),   # real leaf album
            ("BCee", "Loose", "BCee/loose.mp3"),              # loose file at artist root
        ])
        s.commit()
        # A real album folder holds tracks directly → leaf.
        assert library_index.folder_has_nested_tracks(s, 1, "BCee/Best Of") is False
        # The artist root contains a sub-album → not a leaf (whole-folder trash unsafe).
        assert library_index.folder_has_nested_tracks(s, 1, "BCee") is True


def test_folder_queries_treat_underscore_literally_not_as_like_wildcard():
    # Folder names legitimately contain `_` (metadata `/` is mapped to `_`), which is a LIKE
    # single-char wildcard — the queries must escape it so they don't over-match a sibling
    # whose name differs only in that position.
    with _mem_session() as s:
        record_tracks(s, 1, [
            ("AC_DC", "Song", "AC_DC/Album/01 - Song.mp3"),   # target folder (has `_`)
            ("ACxDC", "Other", "ACxDC/Album/01 - Other.mp3"),  # would match `AC_DC%` if unescaped
        ])
        s.commit()
        # folder_has_nested_tracks("AC_DC/Album") must not be tricked by the `_`.
        assert library_index.folder_has_nested_tracks(s, 1, "AC_DC/Album") is False
        removed = library_index.remove_by_prefix(s, 1, "AC_DC")
        s.commit()
        assert removed == 1                                    # only the real AC_DC row
        assert set(library_index.load_index_paths(s, 1).values()) == \
            {"ACxDC/Album/01 - Other.mp3"}                     # sibling untouched
