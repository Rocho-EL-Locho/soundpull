"""Index-aware WebDAV file operations + trash safety net (roadmap 01).

Exercised against a FAKE webdav client (an in-memory "filesystem" that records the same
operations webdav4 exposes) and a real in-memory SQLite session, so the tests assert both
the remote move/delete AND the ServerTrack index staying in sync — without any network.
"""
from contextlib import contextmanager
from datetime import date, timedelta

import app.models  # noqa: F401 — registers tables on SQLModel.metadata
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db
from app import library_ops
from app.library_ops import TRASH_DIR, trash_rel
from app.models import ServerTrack, UserSettings


class FakeClient:
    """Minimal in-memory stand-in for a webdav4 Client (files as full path strings)."""

    def __init__(self, files=()):
        self.files = set(files)
        self.dirs: set[str] = set()

    def _all_dirs(self) -> set[str]:
        d = set(self.dirs)
        for f in self.files:
            parts = f.split("/")
            for i in range(1, len(parts)):
                d.add("/".join(parts[:i]))
        return d

    def exists(self, path: str) -> bool:
        path = path.rstrip("/")
        return path in self.files or path in self._all_dirs()

    def mkdir(self, path: str) -> None:
        self.dirs.add(path.rstrip("/"))

    def move(self, src: str, dst: str, overwrite: bool = False) -> None:
        src, dst = src.rstrip("/"), dst.rstrip("/")
        if dst in self.files and not overwrite:
            raise FileExistsError(dst)
        if src in self.files:
            self.files.discard(src)
            self.files.add(dst)
            return
        prefix = src + "/"
        moved = False
        for f in list(self.files):
            if f.startswith(prefix):
                self.files.discard(f)
                self.files.add(dst + "/" + f[len(prefix):])
                moved = True
        if not moved:
            raise FileNotFoundError(src)

    def remove(self, path: str) -> None:
        path = path.rstrip("/")
        if path in self.files:
            self.files.discard(path)
            return
        prefix = path + "/"
        for f in list(self.files):
            if f.startswith(prefix):
                self.files.discard(f)
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(prefix)}

    def ls(self, path: str, detail: bool = True):
        path = (path or "").rstrip("/")
        prefix = path + "/" if path else ""
        children: dict[str, str] = {}
        for f in self.files | self._all_dirs():
            is_file = f in self.files
            if path and not f.startswith(prefix):
                continue
            if not path and f == "":
                continue
            rest = f[len(prefix):]
            if not rest:
                continue
            if "/" in rest:
                children[prefix + rest.split("/")[0]] = "directory"
            else:
                children.setdefault(prefix + rest, "file" if is_file else "directory")
        return [{"name": name, "type": typ} for name, typ in children.items()]


@pytest.fixture
def env(monkeypatch):
    """Wire a fake client + in-memory DB seeded with a WebDAV-configured user (base=lib)."""
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
    return engine, client, scope


def _set_retention(scope, days: int) -> None:
    with scope() as s:
        us = s.exec(select(UserSettings).where(UserSettings.user_id == 1)).first()
        us.trash_retention_days = days
        s.add(us)


def _seed_track(scope, client, rel: str) -> None:
    client.files.add(f"lib/{rel}")
    with scope() as s:
        from app.library_index import record_tracks
        parts = [p for p in rel.split("/") if p]
        from app.library_index import _artist_title_from_path
        artist, title = _artist_title_from_path(parts)
        record_tracks(s, 1, [(artist, title, rel)])


def test_trash_rel_construction():
    assert trash_rel("A/B/x.mp3", date(2026, 7, 14)) == f"{TRASH_DIR}/2026-07-14/A/B/x.mp3"


def test_trash_track_moves_to_dated_folder_and_drops_index(env):
    engine, client, scope = env
    rel = "Artist/Album/01 - Song.mp3"
    _seed_track(scope, client, rel)

    trel = library_ops.trash_track(1, rel)

    assert trel == trash_rel(rel, date.today())
    assert f"lib/{rel}" not in client.files            # original gone
    assert f"lib/{trel}" in client.files               # now in dated trash folder
    with scope() as s:
        assert s.exec(select(ServerTrack).where(ServerTrack.user_id == 1)).first() is None


def test_trash_track_retention_zero_deletes_immediately(env):
    engine, client, scope = env
    _set_retention(scope, 0)
    rel = "Artist/Album/01 - Song.mp3"
    _seed_track(scope, client, rel)

    result = library_ops.trash_track(1, rel)

    assert result is None
    assert f"lib/{rel}" not in client.files
    # No trash folder was created.
    assert not any(f.startswith(f"lib/{TRASH_DIR}/") for f in client.files)


def test_restore_track_moves_back_and_reindexes(env):
    engine, client, scope = env
    rel = "Artist/Album/01 - Song.mp3"
    _seed_track(scope, client, rel)
    trel = library_ops.trash_track(1, rel)

    restored = library_ops.restore_track(1, trel)

    assert restored == rel
    assert f"lib/{rel}" in client.files
    assert f"lib/{trel}" not in client.files
    with scope() as s:
        row = s.exec(select(ServerTrack).where(ServerTrack.user_id == 1)).first()
        assert row is not None and row.rel_path == rel


def test_move_track_updates_index_path(env):
    engine, client, scope = env
    src = "Artist/Album/01 - Song.mp3"
    dst = "Artist/Best Of/01 - Song.mp3"
    _seed_track(scope, client, src)

    library_ops.move_track(1, src, dst)

    assert f"lib/{dst}" in client.files and f"lib/{src}" not in client.files
    with scope() as s:
        row = s.exec(select(ServerTrack).where(ServerTrack.user_id == 1)).first()
        assert row.rel_path == dst


def test_list_trash_enumerates_entries(env):
    engine, client, scope = env
    _seed_track(scope, client, "Artist/Album/01 - Song.mp3")
    _seed_track(scope, client, "Artist/Album/02 - Other.mp3")
    library_ops.trash_track(1, "Artist/Album/01 - Song.mp3")
    library_ops.trash_track(1, "Artist/Album/02 - Other.mp3")

    entries = library_ops.list_trash(1)

    assert {e.original_rel for e in entries} == {"Artist/Album/01 - Song.mp3",
                                                 "Artist/Album/02 - Other.mp3"}
    assert all(e.date == date.today().isoformat() for e in entries)


def test_list_trash_empty_when_no_trash(env):
    engine, client, scope = env
    assert library_ops.list_trash(1) == []


def test_purge_trash_respects_retention_cutoff(env):
    engine, client, scope = env
    old = (date.today() - timedelta(days=40)).isoformat()
    recent = (date.today() - timedelta(days=5)).isoformat()
    client.files.add(f"lib/{TRASH_DIR}/{old}/Artist/Album/x.mp3")
    client.files.add(f"lib/{TRASH_DIR}/{recent}/Artist/Album/y.mp3")

    removed = library_ops.purge_trash(1)  # retention 30 → cutoff today-30

    assert removed == 1
    assert not any(f"/{old}/" in f for f in client.files)      # expired folder gone
    assert any(f"/{recent}/" in f for f in client.files)       # recent folder kept


def test_purge_trash_force_all(env):
    engine, client, scope = env
    recent = (date.today() - timedelta(days=1)).isoformat()
    client.files.add(f"lib/{TRASH_DIR}/{recent}/Artist/Album/y.mp3")

    removed = library_ops.purge_trash(1, force_all=True)

    assert removed == 1


# --- folder-level trash (roadmap 03) ---------------------------------------

def test_trash_folder_moves_whole_album_and_drops_index(env):
    engine, client, scope = env
    _seed_track(scope, client, "Artist/Album/01 - Song.mp3")
    _seed_track(scope, client, "Artist/Album/02 - Other.mp3")
    # A sibling album must survive untouched.
    _seed_track(scope, client, "Artist/Other/01 - Keep.mp3")

    trel = library_ops.trash_folder(1, "Artist/Album")

    assert trel == trash_rel("Artist/Album", date.today())
    assert f"lib/{trel}/01 - Song.mp3" in client.files
    assert f"lib/{trel}/02 - Other.mp3" in client.files
    assert not any(f == "lib/Artist/Album/01 - Song.mp3" for f in client.files)
    assert "lib/Artist/Other/01 - Keep.mp3" in client.files  # sibling kept
    with scope() as s:
        rows = s.exec(select(ServerTrack).where(ServerTrack.user_id == 1)).all()
        assert {r.rel_path for r in rows} == {"Artist/Other/01 - Keep.mp3"}


def test_trash_folder_retention_zero_hard_deletes(env):
    engine, client, scope = env
    _set_retention(scope, 0)
    _seed_track(scope, client, "Artist/Album/01 - Song.mp3")

    result = library_ops.trash_folder(1, "Artist/Album")

    assert result is None
    assert not any(f.startswith("lib/Artist/Album/") for f in client.files)
    assert not any(f.startswith(f"lib/{TRASH_DIR}/") for f in client.files)
    with scope() as s:
        assert s.exec(select(ServerTrack).where(ServerTrack.user_id == 1)).first() is None


def test_trash_folder_rejects_empty(env):
    engine, client, scope = env
    with pytest.raises(ValueError):
        library_ops.trash_folder(1, "")
    with pytest.raises(ValueError):
        library_ops.trash_folder(1, "/")


def test_trash_folder_refuses_folder_with_subalbums(env):
    # An artist-root "pseudo-album" (loose files beside real sub-albums) must NOT be
    # trashable as a whole — it would sweep the sibling album into the trash too.
    engine, client, scope = env
    _seed_track(scope, client, "Artist/loose.mp3")            # loose file at artist root
    _seed_track(scope, client, "Artist/Real Album/01 - Keep.mp3")  # a real sub-album

    with pytest.raises(ValueError):
        library_ops.trash_folder(1, "Artist")

    # Nothing moved; both files and both index rows are intact.
    assert "lib/Artist/loose.mp3" in client.files
    assert "lib/Artist/Real Album/01 - Keep.mp3" in client.files
    with scope() as s:
        rows = s.exec(select(ServerTrack).where(ServerTrack.user_id == 1)).all()
        assert len(rows) == 2
    assert not any(f.startswith(f"lib/{TRASH_DIR}/") for f in client.files)


def test_purge_ignores_non_dated_folders(env):
    engine, client, scope = env
    client.files.add(f"lib/{TRASH_DIR}/not-a-date/x.mp3")
    assert library_ops.purge_trash(1, force_all=True) == 0
    assert f"lib/{TRASH_DIR}/not-a-date/x.mp3" in client.files


@pytest.mark.parametrize("bad", ["../escape.mp3", "/etc/passwd", "a/../../x.mp3", ""])
def test_operations_reject_traversal_before_network(env, bad):
    engine, client, scope = env
    client_calls = list(client.files)
    with pytest.raises(ValueError):
        library_ops.trash_track(1, bad)
    # Nothing on the fake filesystem changed — the guard fired before any client call.
    assert list(client.files) == client_calls
