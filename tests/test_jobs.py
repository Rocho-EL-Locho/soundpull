"""Job worker helpers (issue #21)."""
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from app import jobs
from app.jobs import (
    _INDEX_WARNING_KEY, _PARTIAL_KEY, _clean_error, _delivery_warning, _record_delivered_safe,
)
from app.pipeline import Result


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))


def test_delivery_warning_clean_run_has_no_warning():
    # Every expected track delivered and the index wrote → no note at all.
    assert _delivery_warning(Result(expected_count=10, new_track_count=10)) == (None, 0, 0)


def test_delivery_warning_index_failure_only():
    # Full delivery but the server-index write failed → the stale-index note (#38).
    key, total, failed = _delivery_warning(Result(new_track_count=10), index_ok=False)
    assert (key, total, failed) == (_INDEX_WARNING_KEY, 0, 0)


def test_delivery_warning_partial_outranks_index_and_carries_counts():
    # Tracks silently dropped (throttle/403) is the important signal — it wins over the
    # index note and reports "9 von 30" so a partial album is visible, not a silent success.
    key, total, failed = _delivery_warning(
        Result(expected_count=30, new_track_count=9, failed_count=21), index_ok=False)
    assert (key, total, failed) == (_PARTIAL_KEY, 30, 21)


def test_delivery_warning_counts_upload_failures_and_backfills_total():
    # Files the WebDAV server rejected also count as failed; with no download-stage total the
    # displayed total falls back to delivered + failed (8 + 2 = 10).
    key, total, failed = _delivery_warning(Result(new_track_count=8, upload_failed_count=2))
    assert (key, total, failed) == (_PARTIAL_KEY, 10, 2)


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


def _arm_artist_job(monkeypatch, dest_type):
    """Register an artist JobState and stub the DB/index side-effects for _run_artist."""
    from app.fix_music_tags import TagOptions

    js = jobs.JobState(id="art1", user_id=7, url="u", genre="Rap", mode="artist",
                       destination_type=dest_type, tag_options=TagOptions())
    jobs._registry["art1"] = js
    monkeypatch.setattr(jobs, "session_scope", _dummy_scope)
    monkeypatch.setattr(jobs, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(jobs.library_index, "load_index_paths",
                        lambda session, uid: {jobs.library_index.track_key("Song", "Artist"): "p"})
    return js


def test_run_artist_auto_dedups_on_webdav(monkeypatch):
    # Artist runs on WebDAV default to building the on_server closure (dedup defaults on), and
    # the album pool is clamped to the 1–4 range regardless of the env value.
    from app.fix_music_tags import TagOptions
    from app.pipeline import Result

    _arm_artist_job(monkeypatch, "webdav")
    monkeypatch.setattr(jobs.settings, "max_artist_album_concurrency", 9)  # over the cap
    captured = {}
    monkeypatch.setattr(jobs, "run_artist_download",
                        lambda **kw: captured.update(kw) or Result(summary="ok"))

    jobs._run_artist("art1", "u", "Rap", jobs.Destination(type="webdav"),
                     "mp3_320", TagOptions(), None)

    on_server = captured["on_server"]
    assert on_server is not None
    assert on_server("Artist", "Song") is True          # present in the loaded index → skip
    assert on_server("Nobody", "Nothing") is False
    assert captured["album_concurrency"] == 4            # 9 clamped down to the max


def test_run_artist_no_dedup_for_browser(monkeypatch):
    # A browser ZIP has no library to dedup against → on_server stays None (full download).
    from app.fix_music_tags import TagOptions
    from app.pipeline import Result

    _arm_artist_job(monkeypatch, "browser")
    captured = {}
    monkeypatch.setattr(jobs, "run_artist_download",
                        lambda **kw: captured.update(kw) or Result(summary="ok"))

    jobs._run_artist("art1", "u", "Rap", jobs.Destination(type="browser"),
                     "mp3_320", TagOptions(), None)

    assert captured["on_server"] is None


def test_run_artist_dedup_off_skips_reconcile_on_webdav(monkeypatch):
    # The per-download toggle can turn dedup OFF even on WebDAV → on_server stays None so the
    # whole discography is re-downloaded instead of skipping existing tracks.
    from app.fix_music_tags import TagOptions
    from app.pipeline import Result

    _arm_artist_job(monkeypatch, "webdav")
    captured = {}
    monkeypatch.setattr(jobs, "run_artist_download",
                        lambda **kw: captured.update(kw) or Result(summary="ok"))

    jobs._run_artist("art1", "u", "Rap", jobs.Destination(type="webdav"),
                     "mp3_320", TagOptions(), None, dedup=False)

    assert captured["on_server"] is None


# --- event timeline (issue #44) --------------------------------------------

def _bind_mem_db(monkeypatch, *, phase="queued"):
    """In-memory DB with a seeded DownloadHistory row; jobs.session_scope points at it."""
    import app.models  # noqa: F401 - register tables
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from app.models import DownloadHistory

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(DownloadHistory(id="j", user_id=1, url="u", genre="Pop", mode="album",
                              audio_format="mp3_320", destination_type="webdav", phase=phase))
        s.commit()

    @contextmanager
    def scope():
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        finally:
            sess.close()

    monkeypatch.setattr(jobs, "session_scope", scope)
    return engine


def test_on_phase_logs_only_on_transition(monkeypatch):
    # on_phase fires on every progress tick; the timeline must record a phase only when it
    # actually changes, else it floods with thousands of identical "download" lines (issue #44).
    _bind_mem_db(monkeypatch)
    js = jobs.JobState(id="j", user_id=1, url="u", genre="Pop", mode="album",
                       destination_type="webdav")
    reporter = jobs._make_reporter("j", js)

    for _ in range(5):
        reporter.on_phase("download")   # simulate 5 progress ticks in one phase
    reporter.on_phase("tags")

    assert js.phase == "tags"                      # live state always current
    assert len(js.log_lines) == 2                  # only the two real transitions
    assert js.log_lines[0].endswith("download")
    assert js.log_lines[1].endswith("tags")


def test_log_event_is_best_effort_on_persist_failure(monkeypatch):
    # A failed timeline write must be swallowed — it must never propagate and (e.g. in a
    # terminal block) flip a delivered job to "error" or skip its notification (issue #44).
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(jobs, "_persist", boom)
    js = jobs.JobState(id="j", user_id=1, url="u", genre="Pop", mode="album",
                       destination_type="webdav")

    jobs._log_event(js, "done")  # must not raise

    assert js.log_lines and js.log_lines[-1].endswith("done")


# --- scheduled/manual scan guard (roadmap 03) ------------------------------

def test_run_scan_sync_runs_and_releases_slot(monkeypatch):
    calls = []
    monkeypatch.setattr("app.library_index.scan_webdav",
                        lambda uid: calls.append(uid) or (1, 0, []))

    assert jobs.run_scan_sync(7) == (1, 0, [])
    assert calls == [7]
    assert not jobs.is_scan_running(7)          # slot released after the run


def test_run_scan_sync_skips_when_already_running(monkeypatch):
    calls = []
    monkeypatch.setattr("app.library_index.scan_webdav",
                        lambda uid: calls.append(uid) or (1, 0, []))

    jobs._scans_running.add(7)                   # simulate a scan already in flight
    try:
        assert jobs.run_scan_sync(7) is None     # guarded → skipped
        assert calls == []                       # scan_webdav never called
    finally:
        jobs._scans_running.discard(7)


def test_run_scan_sync_releases_slot_on_error(monkeypatch):
    def boom(uid):
        raise RuntimeError("dav down")

    monkeypatch.setattr("app.library_index.scan_webdav", boom)
    try:
        jobs.run_scan_sync(7)
    except RuntimeError:
        pass
    assert not jobs.is_scan_running(7)           # slot released even when the scan raised


# --- batch import (roadmap 12) ---------------------------------------------

def test_run_batch_download_aggregates_counts_and_skips_failures(monkeypatch, tmp_path):
    """One bad item is skipped (folded into failed), the rest deliver into one zip."""
    from app import pipeline
    from app.pipeline import Destination, Reporter, Result

    seen = []

    def fake_run_download(*, job_id, url, stage_dir, **kw):
        seen.append(url)
        if url == "bad":
            raise RuntimeError("unavailable")
        (stage_dir / f"{url}.mp3").write_bytes(b"x")   # stage a real file so zip/any-file passes
        # A browser single reports expected_count=0 (no dedup match-filter) — the batch total must
        # still come out as delivered+failed, not from expected_count.
        return Result(new_track_count=1, expected_count=0, failed_count=0,
                      delivered=[("A", url, f"{url}.mp3")])

    monkeypatch.setattr(pipeline, "run_download", fake_run_download)
    calls = {"track": []}
    rep = Reporter(on_phase=lambda p: None, on_meta=lambda a, b: None,
                   on_track=lambda c, t: calls["track"].append((c, t)))

    res = pipeline.run_batch_download(job_id="test-batch-agg", urls=["a", "bad", "c"],
                                      genre="Rap", destination=Destination(type="browser"),
                                      reporter=rep)

    assert seen == ["a", "bad", "c"]
    assert res.new_track_count == 2                 # two good items delivered
    assert res.expected_count == 3 and res.failed_count == 1   # the raised item counts as failed
    assert res.zip_path and res.zip_name == "Import.zip"
    assert calls["track"][-1] == (3, 3)             # progress reached total
    # `_delivery_warning` then surfaces "1 von 3".
    assert _delivery_warning(res) == (_PARTIAL_KEY, 3, 1)


@contextmanager
def _noop():
    yield


def test_start_batch_writes_one_history_row_and_runs_inline(monkeypatch):
    import json

    import app.db
    import app.models  # noqa: F401
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine, select
    from app.models import DownloadHistory, UserSettings
    from app.pipeline import Result

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(UserSettings(user_id=1))
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
    monkeypatch.setattr(jobs, "session_scope", scope)
    # Run the worker inline instead of on the pool, and stub the actual download.
    monkeypatch.setattr(jobs._executor, "submit", lambda fn, *a: fn(*a))
    monkeypatch.setattr(jobs, "run_batch_download",
                        lambda **kw: Result(summary="Import.zip", new_track_count=2,
                                            expected_count=2, failed_count=0))

    items = ["https://music.youtube.com/watch?v=a", "https://music.youtube.com/watch?v=b"]
    job_id = jobs.start_batch(user_id=1, items=items, genre="Rap", destination_type="browser")

    with scope() as s:
        row = s.get(DownloadHistory, job_id)
        assert row.mode == "batch"
        assert json.loads(row.batch_urls) == items          # retryable
        assert row.phase == "done"
    js = jobs.get_job(job_id)
    assert js.mode == "batch" and js.total_tracks == 2 and js.failed_tracks == 0


def test_run_batch_download_recreates_playlist_on_webdav(monkeypatch, tmp_path):
    """A playlist_spec triggers the import-m3u write on the WebDAV path (browser skips it)."""
    from app import pipeline
    from app.pipeline import Destination, PlaylistSpec, Reporter, Result

    def fake_run_download(*, job_id, url, stage_dir, **kw):
        (stage_dir / f"{url}.mp3").write_bytes(b"x")
        return Result(new_track_count=1, expected_count=0, failed_count=0,
                      delivered=[("Artist", url, f"Artist/Alb/{url}.mp3")])

    monkeypatch.setattr(pipeline, "run_download", fake_run_download)
    monkeypatch.setattr(pipeline, "_upload_tree", lambda dest, root: [])   # skip real WebDAV

    recorded = {}
    real_write = pipeline._write_import_m3u
    def spy(work_base, spec, delivered, index_paths):
        recorded["spec"] = spec
        recorded["delivered"] = delivered
        return real_write(work_base, spec, delivered, index_paths)
    monkeypatch.setattr(pipeline, "_write_import_m3u", spy)

    rep = Reporter(on_phase=lambda p: None, on_meta=lambda a, b: None, on_track=lambda c, t: None)
    spec = PlaylistSpec(name="Mix", folder_id="import-deadbeef01",
                        tracks=[("Artist", "a"), ("Artist", "b")])
    pipeline.run_batch_download(job_id="test-batch-pl", urls=["a", "b"], genre="Rap",
                                destination=Destination(type="webdav"), reporter=rep,
                                playlist_spec=spec, index_paths={})
    assert recorded["spec"].name == "Mix"
    assert len(recorded["delivered"]) == 2
