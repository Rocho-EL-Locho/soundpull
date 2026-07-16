"""Library-wide duplicate finder & cleanup (roadmap 04).

Split into three layers, all network-free:
- pure grouping / keeper heuristic over synthetic walk data (`_build_report`);
- the pure `rewrite_m3u` / `_repoint` playlist-reference repair;
- `analyze` + `resolve_group` against a FAKE webdav client + a real in-memory DB, asserting the
  file moved to trash, the index left with exactly one row at the keeper, and the playlist m3u
  re-pointed — mirroring `test_library_ops`.
"""
from contextlib import contextmanager

import app.models  # noqa: F401 — registers tables on SQLModel.metadata
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db
import app.webdav_util
from app import duplicates, library_ops
from app.duplicates import (PathInfo, _build_report, _dup_key, _is_playlist_folder,
                            _keeper, rewrite_m3u)
from app.models import DuplicateReport, PlaylistSubscription, ServerTrack, UserSettings


# --- pure grouping ---------------------------------------------------------

def _f(rel, artist, title):
    # The duplicate key is derived from the PATH (artist+album folders) + title, so a copy is
    # only a duplicate when artist, album AND title match (not just title+artist).
    return (_dup_key(rel, title), rel, artist, title)


def test_exact_group_found_singles_untouched():
    # A genuine duplicate under the album-aware rule: the SAME album (artist+album+title all
    # equal) present at two paths. A different album with the same title is NOT a duplicate.
    files = [
        _f("Burial/Untrue/05 - Archangel.mp3", "Burial", "Archangel"),
        _f("Backup/Burial/Untrue/05 - Archangel.mp3", "Burial", "Archangel"),
        _f("Foo/Bar/01 - Unique.mp3", "Foo", "Unique"),
    ]
    counts = {"Burial/Untrue": 13, "Backup/Burial/Untrue": 13, "Foo/Bar": 5}
    report = _build_report(files, counts)
    assert len(report.exact) == 1
    assert len(report.probable) == 0
    g = report.exact[0]
    assert (g.artist, g.title) == ("Burial", "Archangel")
    assert {p.rel_path for p in g.paths} == {
        "Burial/Untrue/05 - Archangel.mp3", "Backup/Burial/Untrue/05 - Archangel.mp3"}


def test_same_title_different_album_is_not_a_duplicate():
    # Regression (user report): two DIFFERENT tracks that share a generic title ("01. Intro")
    # in different albums must NOT be flagged — album is part of the key now.
    files = [
        _f("PA Sports - Life is Pain/01. Intro.mp3", "PA Sports", "01. Intro"),
        _f("PA Sports - Machtwechsel II/01. Intro.mp3", "PA Sports", "01. Intro"),
    ]
    counts = {"PA Sports - Life is Pain": 12, "PA Sports - Machtwechsel II": 14}
    report = _build_report(files, counts)
    assert report.exact == []
    assert report.probable == []


def test_keeper_prefers_biggest_album_over_playlist():
    paths = [
        PathInfo("Burial/Untrue/05 - Archangel.mp3", "Burial/Untrue", 13, False),
        PathInfo("Burial/Archangel/01 - Archangel.mp3", "Burial/Archangel", 1, False),
        PathInfo("Chill [PL1]/03 - Archangel.mp3", "Chill [PL1]", 20, True),
    ]
    # Biggest REAL album wins even though the playlist folder has more tracks.
    assert _keeper(paths) == "Burial/Untrue/05 - Archangel.mp3"


def test_keeper_demotes_playlist_folder():
    paths = [
        PathInfo("Chill [PL1]/03 - Song.mp3", "Chill [PL1]", 50, True),
        PathInfo("Artist/Album/01 - Song.mp3", "Artist/Album", 2, False),
    ]
    assert _keeper(paths) == "Artist/Album/01 - Song.mp3"


def test_keeper_tiebreak_shorter_then_lexicographic():
    paths = [
        PathInfo("Artist/Bbb/01 - Song.mp3", "Artist/Bbb", 5, False),
        PathInfo("Artist/Aaa/01 - Song.mp3", "Artist/Aaa", 5, False),
    ]
    # Equal folder size, equal length → lexicographically smaller path wins.
    assert _keeper(paths) == "Artist/Aaa/01 - Song.mp3"


def test_probable_tier_from_noise_only():
    # Noise-variant of the same track WITHIN one album (same artist+album, title differs only
    # by "(Official Video)") → probable, not exact.
    files = [
        _f("X/Album/01 - Song.mp3", "X", "Song"),
        _f("X/Album/09 - Song (Official Video).mp3", "X", "Song (Official Video)"),
    ]
    counts = {"X/Album": 10}
    report = _build_report(files, counts)
    assert len(report.exact) == 0          # different exact keys → not exact
    assert len(report.probable) == 1
    g = report.probable[0]
    assert g.tier == "probable"
    assert g.suggested_keeper == "X/Album/01 - Song.mp3"


def test_feat_variants_collapse_into_exact_key():
    # _dup_key strips "(feat. …)" from the title — a "(feat. B)" copy in the same album collapses
    # onto the clean title, so the two are one exact group (regression guard).
    files = [
        _f("A/Album/01 - Song.mp3", "A", "Song"),
        _f("A/Album/05 - Song (feat. B).mp3", "A / B", "Song (feat. B)"),
    ]
    counts = {"A/Album": 8}
    report = _build_report(files, counts)
    assert len(report.exact) == 1
    assert len(report.probable) == 0


@pytest.mark.parametrize("folder,expected", [
    ("Chill [PLFgquLnL59alW3xmYiWRaoz0]", True),      # real YouTube playlist id
    ("Mix [OLAK5uy_kd9lF3aH8Nq2K1]", True),           # auto-generated album-playlist id
    ("Artist/Greatest Hits [Deluxe Edition]", False),  # album edition suffix, not a playlist
    ("Artist/Album [Remastered]", False),             # ditto (no digit, has letters only)
    ("Artist/Album [2019]", False),                   # year suffix (too short / digits-only)
    ("Artist/Plain Album", False),
])
def test_is_playlist_folder_heuristic(folder, expected):
    assert _is_playlist_folder(folder) is expected


# --- pure m3u repair -------------------------------------------------------

def test_rewrite_m3u_bare_filename_in_folder():
    text = "#EXTM3U\n#EXTINF:-1,A - Song\n01 - Song.mp3\n"
    # The removed copy is in the playlist folder; keeper is a different in-folder file.
    out = rewrite_m3u(text, "Chill [PL1]", {"Chill [PL1]/01 - Song.mp3"},
                      "Chill [PL1]/09 - Song.mp3")
    assert out is not None
    assert "09 - Song.mp3" in out
    assert "01 - Song.mp3" not in out
    assert out.endswith("\n")


def test_rewrite_m3u_cross_folder_reference():
    text = "#EXTM3U\n#EXTINF:-1,A - Song\n../Artist/Single/02 - Song.mp3\n"
    out = rewrite_m3u(text, "Chill [PL1]", {"Artist/Single/02 - Song.mp3"},
                      "Artist/Best Of/09 - Song.mp3")
    assert out is not None
    assert "../Artist/Best Of/09 - Song.mp3" in out


def test_rewrite_m3u_untouched_returns_none():
    text = "#EXTM3U\n#EXTINF:-1,A - Song\n01 - Song.mp3\n"
    assert rewrite_m3u(text, "Chill [PL1]", {"Nowhere/x.mp3"}, "Y/z.mp3") is None


def test_rewrite_m3u_preserves_comments_and_other_lines():
    text = "#EXTM3U\n#PLAYLIST:Mix\n#EXTINF:-1,A - Keep\nkeepme.mp3\n#EXTINF:-1,B\nold.mp3\n"
    out = rewrite_m3u(text, "PL [X]", {"PL [X]/old.mp3"}, "PL [X]/new.mp3")
    assert out is not None
    assert "#PLAYLIST:Mix" in out
    assert "keepme.mp3" in out          # unrelated track untouched
    assert "new.mp3" in out
    assert "old.mp3" not in out


# --- analyze + resolve against a fake client -------------------------------

class FakeClient:
    """In-memory webdav4 stand-in: text files stored as bytes, dirs derived from paths."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def _all_dirs(self):
        d = set()
        for f in self.files:
            parts = f.split("/")
            for i in range(1, len(parts)):
                d.add("/".join(parts[:i]))
        return d

    def exists(self, path):
        path = path.rstrip("/")
        return path in self.files or path in self._all_dirs()

    def mkdir(self, path):
        pass

    def move(self, src, dst, overwrite=False):
        src, dst = src.rstrip("/"), dst.rstrip("/")
        if src in self.files:
            self.files[dst] = self.files.pop(src)
            return
        prefix = src + "/"
        for f in list(self.files):
            if f.startswith(prefix):
                self.files[dst + "/" + f[len(prefix):]] = self.files.pop(f)

    def remove(self, path):
        path = path.rstrip("/")
        self.files.pop(path, None)
        prefix = path + "/"
        for f in list(self.files):
            if f.startswith(prefix):
                self.files.pop(f, None)

    def ls(self, path, detail=True):
        path = (path or "").rstrip("/")
        prefix = path + "/" if path else ""
        children: dict[str, str] = {}
        for f in set(self.files) | self._all_dirs():
            if path and not f.startswith(prefix):
                continue
            rest = f[len(prefix):]
            if not rest:
                continue
            if "/" in rest:
                children[prefix + rest.split("/")[0]] = "directory"
            else:
                children.setdefault(prefix + rest, "file" if f in self.files else "directory")
        return [{"name": n, "type": ty} for n, ty in children.items()]

    def download_fileobj(self, path, fileobj):
        fileobj.write(self.files[path.rstrip("/")])

    def upload_fileobj(self, fileobj, path, overwrite=False):
        self.files[path.rstrip("/")] = fileobj.read()


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(UserSettings(user_id=1, webdav_url="https://dav.example", webdav_folder="lib",
                           trash_retention_days=30))
        s.commit()

    @contextmanager
    def scope():
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        finally:
            sess.close()

    monkeypatch.setattr(app.db, "session_scope", scope)
    client = FakeClient()
    monkeypatch.setattr(library_ops, "make_client", lambda *a, **k: client)
    monkeypatch.setattr(app.webdav_util, "make_client", lambda *a, **k: client)
    return client, scope


def _add_audio(client, rel):
    client.files[f"lib/{rel}"] = b"\x00"


def _index(scope, rel):
    with scope() as s:
        from app.library_index import _artist_title_from_path, record_tracks
        artist, title = _artist_title_from_path([p for p in rel.split("/") if p])
        record_tracks(s, 1, [(artist, title, rel)])


def test_analyze_finds_exact_group_and_persists(env):
    client, scope = env
    # Same album (Burial/Untrue) present at two paths → an album-aware exact duplicate.
    _add_audio(client, "Burial/Untrue/05 - Archangel.mp3")
    _add_audio(client, "Backup/Burial/Untrue/05 - Archangel.mp3")

    report = duplicates.analyze(1)

    assert len(report.exact) == 1
    g = report.exact[0]
    assert g.suggested_keeper == "Burial/Untrue/05 - Archangel.mp3"  # shorter path wins the tie
    # Persisted (one row per user) and reloadable.
    with scope() as s:
        assert s.exec(select(DuplicateReport).where(DuplicateReport.user_id == 1)).first()
    reloaded = duplicates.load_report(1)
    assert len(reloaded.exact) == 1


def test_resolve_group_trashes_loser_and_fixes_index(env):
    client, scope = env
    keeper = "Burial/Untrue/05 - Archangel.mp3"
    loser = "Burial/Archangel/01 - Archangel.mp3"
    _add_audio(client, keeper)
    _add_audio(client, loser)
    # Seed the index pointing at the LOSER (the tricky case: trashing it drops the row).
    _index(scope, loser)

    result = duplicates.resolve_group(1, keeper, [loser])

    assert result.trashed == [loser]
    assert f"lib/{keeper}" in client.files                 # keeper untouched
    assert f"lib/{loser}" not in client.files              # loser moved out
    assert any(f.startswith(f"lib/{library_ops.TRASH_DIR}/") for f in client.files)  # to trash
    # Exactly one index row, now pointing at the keeper → a re-scan won't resurrect the dup.
    with scope() as s:
        rows = s.exec(select(ServerTrack).where(ServerTrack.user_id == 1)).all()
        assert len(rows) == 1
        assert rows[0].rel_path == keeper


def test_resolve_group_repoints_playlist_m3u(env):
    client, scope = env
    keeper = "Burial/Untrue/05 - Archangel.mp3"
    loser = "Burial/Archangel/01 - Archangel.mp3"
    _add_audio(client, keeper)
    _add_audio(client, loser)
    _index(scope, keeper)
    # A playlist folder whose .m3u8 references the LOSER by a cross-folder relative path.
    m3u = ("#EXTM3U\n#PLAYLIST:Late Night\n#EXTINF:-1,Burial - Archangel\n"
           "../Burial/Archangel/01 - Archangel.mp3\n")
    client.files["lib/Late Night [PL9]/Late Night.m3u8"] = m3u.encode("utf-8")

    result = duplicates.resolve_group(1, keeper, [loser])

    assert "Late Night [PL9]/Late Night.m3u8" in result.m3u_repaired
    new = client.files["lib/Late Night [PL9]/Late Night.m3u8"].decode("utf-8")
    assert "../Burial/Untrue/05 - Archangel.mp3" in new
    assert "Archangel/01 - Archangel.mp3" not in new


def test_list_playlist_files_skips_trash_and_cache(env):
    client, scope = env
    client.files["lib/Chill [PL1]/Chill.m3u8"] = b"#EXTM3U\n"
    client.files["lib/.soundpull-trash/2026-07-14/Old [PL2]/Old.m3u8"] = b"#EXTM3U\n"
    client.files["lib/__sized__/deadbeef/thumb.m3u8"] = b"#EXTM3U\n"  # cache shard

    rels = library_ops.list_playlist_files(1)

    assert rels == ["Chill [PL1]/Chill.m3u8"]  # trash + cache pruned


def test_resolve_group_repairs_subscription_manifest(env):
    client, scope = env
    import json
    keeper = "Burial/Untrue/05 - Archangel.mp3"
    loser = "Burial/Archangel/01 - Archangel.mp3"
    _add_audio(client, keeper)
    _add_audio(client, loser)
    _index(scope, keeper)
    # The on-disk m3u the subscription owns (so the folder is matched by manifest content).
    client.files["lib/Late Night [PL9]/Late Night.m3u8"] = (
        "#EXTM3U\n../Burial/Archangel/01 - Archangel.mp3\n").encode("utf-8")
    manifest = [{"index": 1, "name": "../Burial/Archangel/01 - Archangel.mp3",
                 "title": "Archangel", "artist": "Burial", "dur": -1}]
    with scope() as s:
        s.add(PlaylistSubscription(user_id=1, url="https://x", name="Late Night",
                                   playlist_files=json.dumps(manifest)))
        s.commit()

    result = duplicates.resolve_group(1, keeper, [loser])

    assert result.manifests_repaired == 1
    with scope() as s:
        sub = s.exec(select(PlaylistSubscription).where(
            PlaylistSubscription.user_id == 1)).first()
        entries = json.loads(sub.playlist_files)
    assert entries[0]["name"] == "../Burial/Untrue/05 - Archangel.mp3"


def test_manifest_repair_matches_by_content_despite_title_drift(env):
    # The subscription's cached name ("Old Title") no longer matches the on-disk folder/m3u name
    # ("Late Night") — a name-based match would fail; a content-based match still finds it.
    client, scope = env
    import json
    keeper = "Burial/Untrue/05 - Archangel.mp3"
    loser = "Burial/Archangel/01 - Archangel.mp3"
    _add_audio(client, keeper)
    _add_audio(client, loser)
    _index(scope, keeper)
    client.files["lib/Late Night [PL9]/Late Night.m3u8"] = (
        "#EXTM3U\n../Burial/Archangel/01 - Archangel.mp3\n").encode("utf-8")
    manifest = [{"index": 1, "name": "../Burial/Archangel/01 - Archangel.mp3",
                 "title": "Archangel", "artist": "Burial", "dur": -1}]
    with scope() as s:
        s.add(PlaylistSubscription(user_id=1, url="https://x", name="Old Title",
                                   playlist_files=json.dumps(manifest)))
        s.commit()

    result = duplicates.resolve_group(1, keeper, [loser])

    assert result.manifests_repaired == 1
    with scope() as s:
        sub = s.exec(select(PlaylistSubscription).where(
            PlaylistSubscription.user_id == 1)).first()
        assert json.loads(sub.playlist_files)[0]["name"] == "../Burial/Untrue/05 - Archangel.mp3"


def test_save_report_updates_groups_without_restamping(env):
    client, scope = env
    _add_audio(client, "A/Alb/01 - S.mp3")
    _add_audio(client, "A/Sng/01 - S.mp3")
    report = duplicates.analyze(1)
    with scope() as s:
        created_before = s.exec(select(DuplicateReport).where(
            DuplicateReport.user_id == 1)).first().created_at

    report.exact = []                       # simulate a resolve pruning the only group
    duplicates.save_report(1, report)

    reloaded = duplicates.load_report(1)
    assert reloaded.exact == []             # persisted pruned state (no stale group on reload)
    with scope() as s:
        assert s.exec(select(DuplicateReport).where(
            DuplicateReport.user_id == 1)).first().created_at == created_before  # not re-stamped
