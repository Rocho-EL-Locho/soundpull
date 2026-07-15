"""Library health check & repair (roadmap 05).

Three layers, all network-free:
- pure cheap detectors (H1–H4) + `earliest_date` over synthetic data;
- `iter_library_dirs` traversal over a fake client;
- deep checks/fixes (H5 year / H6 cover / H7 genre / H9 decode) on real ffmpeg-built audio, and a
  `deep_check_batch` / `fix_album` end-to-end through a fake WebDAV client (resumability + trash).

ffmpeg-dependent cases are skipped when ffmpeg is absent (it is a hard runtime dep, present in CI).
`fix_music_tags` is never routed through — parity holds by construction.
"""
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import app.models  # noqa: F401 — registers tables
import pytest
from mutagen import File as MutagenFile
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db
import app.webdav_util
from app import health, library_index, library_ops
from app.health import Finding, detect_cheap, earliest_date
from app.models import HealthReport, UserSettings

_FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg not on PATH")


# --- pure cheap detectors --------------------------------------------------

def test_cheap_detects_all_classes():
    entries = [
        ("", ["Artist"], []),
        ("Artist/Album", [], ["01 - X.mp3", "01 - X.lrc", "cover.jpg",
                               "02 - Y.mp3", "thumb.webp", "frag.part", "notes.txt"]),
        ("Empty", [], []),
    ]
    got = {(f.check_id, f.rel_path) for f in detect_cheap(entries, lyrics_enabled=True)}
    assert ("lyrics_missing", "Artist/Album/02 - Y.mp3") in got   # H1: no sibling .lrc
    assert ("lyrics_missing", "Artist/Album/01 - X.mp3") not in got  # has .lrc
    assert ("stray_file", "Artist/Album/thumb.webp") in got       # H2 thumbnail
    assert ("stray_file", "Artist/Album/frag.part") in got        # H2 fragment
    assert ("junk_file", "Artist/Album/notes.txt") in got         # H4 unknown
    assert ("empty_folder", "Empty") in got                       # H3
    # cover.jpg is allowed art, never flagged
    assert not any(rel.endswith("cover.jpg") for _cid, rel in got)


def test_cheap_lyrics_gated_off():
    entries = [("A/B", [], ["01.mp3"])]
    ids = {f.check_id for f in detect_cheap(entries, lyrics_enabled=False)}
    assert "lyrics_missing" not in ids


def test_empty_base_never_flagged():
    # The base itself (dir_rel == "") is never an "empty folder" finding.
    assert detect_cheap([("", [], [])], lyrics_enabled=True) == []


def test_earliest_date():
    assert earliest_date(["2021", "2019", "2020"]) == "2019"
    assert earliest_date(["2020"]) == "2020"
    assert earliest_date(["", None]) is None
    assert earliest_date([]) is None


# --- iter_library_dirs -----------------------------------------------------

class FakeClient:
    """In-memory webdav4 stand-in: remote path -> bytes (dirs derived from paths)."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def _dirs(self):
        d = set()
        for f in self.files:
            p = f.split("/")
            for i in range(1, len(p)):
                d.add("/".join(p[:i]))
        return d

    def exists(self, path):
        path = path.rstrip("/")
        return path in self.files or path in self._dirs()

    def ls(self, path, detail=True):
        path = (path or "").rstrip("/")
        pre = path + "/" if path else ""
        ch: dict[str, str] = {}
        for f in set(self.files) | self._dirs():
            if path and not f.startswith(pre):
                continue
            rest = f[len(pre):]
            if not rest:
                continue
            if "/" in rest:
                ch[pre + rest.split("/")[0]] = "directory"
            else:
                ch.setdefault(pre + rest, "file" if f in self.files else "directory")
        return [{"name": n, "type": t} for n, t in ch.items()]

    def download_file(self, remote, local):
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(self.files[remote.rstrip("/")])

    def upload_file(self, local, remote, overwrite=False):
        self.files[remote.rstrip("/")] = Path(local).read_bytes()

    def move(self, src, dst, overwrite=False):
        src, dst = src.rstrip("/"), dst.rstrip("/")
        if src in self.files:
            self.files[dst] = self.files.pop(src)

    def remove(self, path):
        path = path.rstrip("/")
        self.files.pop(path, None)
        for f in [x for x in self.files if x.startswith(path + "/")]:
            self.files.pop(f, None)

    def mkdir(self, path):
        pass


def test_iter_library_dirs_reports_files_subdirs_and_prunes_cache():
    c = FakeClient()
    for f in ["lib/Artist/Album/01.mp3", "lib/Artist/Album/cover.jpg",
              "lib/__sized__/a/t.jpg", "lib/PL [PLx]/00.mp3"]:
        c.files[f] = b"x"
    seen = {d: (sorted(subs), sorted(files))
            for d, subs, files in library_index.iter_library_dirs(c, "lib")}
    assert seen["Artist/Album"] == ([], ["01.mp3", "cover.jpg"])
    assert "Artist" in seen[""][0] and "PL [PLx]" in seen[""][0]
    assert "Artist/__sized__" not in seen and "__sized__" not in seen  # cache subtree pruned


# --- deep checks / fixes on real audio -------------------------------------

def _mk(path: Path, *, date=None, genre=None, cover=False):
    args = [_FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.2"]
    if date:
        args += ["-metadata", f"date={date}"]
    if genre:
        args += ["-metadata", f"genre={genre}"]
    args += ["-codec:a", "libmp3lame", str(path)]
    subprocess.run(args, check=True, capture_output=True)
    if cover:
        health._embed_cover(path, b"\xff\xd8\xff\xe0JUNKJPEG")  # embed dummy art


@needs_ffmpeg
def test_fix_year_unifies_to_earliest_and_only_touches_changed(tmp_path):
    a, b, c = tmp_path / "a.mp3", tmp_path / "b.mp3", tmp_path / "c.mp3"
    _mk(a, date="2019", genre="Rap")
    _mk(b, date="2021", genre="Rap")
    _mk(c, date="2019", genre="Rap")
    staged = [("A/a.mp3", a), ("A/b.mp3", b), ("A/c.mp3", c)]

    changed = health._fix_year(staged)

    assert changed == {b}                                   # only the 2021 file changed
    for p in (a, b, c):
        assert MutagenFile(str(p), easy=True)["date"] == ["2019"]
    assert MutagenFile(str(a), easy=True)["genre"] == ["Rap"]   # other tags untouched


@needs_ffmpeg
def test_fix_year_noop_single_year(tmp_path):
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    _mk(a, date="2020")
    _mk(b, date="2020")
    assert health._fix_year([("A/a.mp3", a), ("A/b.mp3", b)]) == set()


@needs_ffmpeg
def test_cover_presence_and_embed(tmp_path):
    bare, withart = tmp_path / "bare.mp3", tmp_path / "art.mp3"
    _mk(bare)
    _mk(withart, cover=True)
    assert health._has_cover(bare) is False
    assert health._has_cover(withart) is True

    changed = health._fix_cover_local([("A/bare.mp3", bare), ("A/art.mp3", withart)],
                                      b"\xff\xd8\xff\xe0COVERBYTES")
    assert changed == {bare}                       # only the coverless file got art
    assert health._has_cover(bare) is True


@needs_ffmpeg
def test_fix_genre_writes_default_only_when_missing(tmp_path):
    none, has = tmp_path / "n.mp3", tmp_path / "h.mp3"
    _mk(none)
    _mk(has, genre="Jazz")
    changed = health._fix_genre([("A/n.mp3", none), ("A/h.mp3", has)], "Rap")
    assert changed == {none}
    assert MutagenFile(str(none), easy=True)["genre"] == ["Rap"]
    assert MutagenFile(str(has), easy=True)["genre"] == ["Jazz"]   # untouched


@needs_ffmpeg
def test_decode_error_flags_corrupt_only(tmp_path):
    good, bad = tmp_path / "good.mp3", tmp_path / "bad.mp3"
    _mk(good)
    bad.write_bytes(b"not an audio file at all" * 100)
    assert health._decode_error(good) is None
    assert health._decode_error(bad) is not None


@needs_ffmpeg
def test_detect_album_finds_year_split_and_missing_tags(tmp_path):
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    _mk(a, date="2019")            # no genre, no cover
    _mk(b, date="2021", genre="Rap", cover=True)
    findings = health._detect_album("Artist/Album", [("Artist/Album/a.mp3", a),
                                                     ("Artist/Album/b.mp3", b)])
    ids = {(f.check_id, f.rel_path) for f in findings}
    assert ("year_split", "Artist/Album") in ids
    assert ("cover_missing", "Artist/Album/a.mp3") in ids
    assert ("cover_missing", "Artist/Album/b.mp3") not in ids
    assert ("genre_missing", "Artist/Album/a.mp3") in ids


# --- report persistence + resumability -------------------------------------

@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(UserSettings(user_id=1, webdav_url="https://d", webdav_folder="lib",
                           trash_retention_days=30, default_genre="Rap",
                           fetch_synced_lyrics=True))
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
    monkeypatch.setattr(app.webdav_util, "make_client", lambda *a, **k: client)
    monkeypatch.setattr(library_ops, "make_client", lambda *a, **k: client)
    return client, scope


def test_cheap_run_persists_and_reloads(env):
    client, scope = env
    client.files["lib/Artist/Album/01 - X.mp3"] = b"x"       # no .lrc → H1
    client.files["lib/Artist/Album/thumb.jpg"] = b"x"        # H2 stray

    report = health.run_cheap_checks(1)

    ids = {f.check_id for f in report.cheap}
    assert "lyrics_missing" in ids and "stray_file" in ids
    with scope() as s:
        assert s.exec(select(HealthReport).where(HealthReport.user_id == 1)).first()
    assert {f.check_id for f in health.load_report(1).cheap} == ids


def test_deep_batch_is_resumable_and_bounded(env):
    client, scope = env
    for alb in ("A1", "A2", "A3"):
        client.files[f"lib/{alb}/01.mp3"] = b"x"

    health.deep_check_batch(1, limit=2)
    rep = health.load_report(1)
    assert len(rep.checked_albums) == 2                      # bound respected

    health.deep_check_batch(1, limit=2)
    rep = health.load_report(1)
    assert sorted(rep.checked_albums) == ["A1", "A2", "A3"]  # continued where it stopped


@needs_ffmpeg
def test_fix_album_year_end_to_end_reuploads_only_changed(env, tmp_path):
    client, scope = env
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    _mk(a, date="2019", genre="Rap")
    _mk(b, date="2021", genre="Rap")
    client.files["lib/A/01.mp3"] = a.read_bytes()
    client.files["lib/A/02.mp3"] = b.read_bytes()

    res = health.fix_album(1, "A", {"year_split"})

    assert res.ok
    assert "A" in res.fixed_paths and "A/02.mp3" in res.fixed_paths
    assert "A/01.mp3" not in res.fixed_paths            # already earliest → not re-uploaded
    # Re-download the re-uploaded file and confirm the year was unified.
    out = tmp_path / "check.mp3"
    out.write_bytes(client.files["lib/A/02.mp3"])
    assert MutagenFile(str(out), easy=True)["date"] == ["2019"]


def test_stray_fix_trashes_file(env):
    client, scope = env
    client.files["lib/Artist/Album/thumb.jpg"] = b"x"
    res = health.fix_finding(1, "stray_file", "Artist/Album/thumb.jpg")
    assert res.ok
    assert "lib/Artist/Album/thumb.jpg" not in client.files
    assert any(f.startswith(f"lib/{library_ops.TRASH_DIR}/") for f in client.files)
