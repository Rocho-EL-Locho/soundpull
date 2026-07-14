# 15 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names. **This touches the
job machinery's core — smallest possible mechanical change, read `app/jobs.py`
fully first.**

## Touch points

| File | Change |
|---|---|
| `app/jobs.py` | pending deque + dispatcher; cancel flag; `cancelled` phase |
| `app/pipeline.py` | cooperative cancel checks (hook, match_filter, between phases); `CancelToken` param |
| `app/pages/index.py` | cancel/reorder controls + queue position on job cards |
| `app/pages/history.py` | `cancelled` phase in filters/labels; cancel from detail dialog |
| `app/models.py` | none (phase is a string column already) |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_jobs.py`, `tests/test_pipeline.py` | see Testing |

## Step plan

### 1. Queue mechanics (`app/jobs.py`)

```python
_pending: deque[str] = deque()          # job ids, guarded by _queue_lock
_active: set[str] = set()

def _dispatch() -> None:
    # under lock: while len(_active) < max_concurrent and _pending:
    #   pop left, add to _active, executor.submit(_wrapped_run, job_id)

def _wrapped_run(job_id):
    try: <existing _run/_run_artist/_run_sync dispatch>
    finally: _active.discard(job_id); _dispatch()
```

- `start_job`/`start_sync` append to `_pending` + call `_dispatch` instead of
  submitting directly. `JobState.phase` for pending jobs stays `queued` (exists),
  with a computed queue position exposed via a helper (`queue_position(job_id)`).
- `cancel_job(job_id)`:
  - pending → remove from deque, phase `cancelled`, `_persist`, done.
  - active → set `JobState.cancel_requested = True` (new dataclass field); the
    pipeline does the rest.
- Locking: one module lock around deque/active mutations; keep critical sections
  tiny (no DB/network under the lock).
- `reorder_job(job_id, direction)` / `move_to_front`: deque manipulation under the
  same lock; no-op if the job started meanwhile (races are benign).

### 2. Cooperative cancel (`app/pipeline.py`)

- `run_download` / `run_artist_download` gain `cancel: Callable[[], bool] | None`
  (jobs passes `lambda: state.cancel_requested`).
- Check points:
  - inside the progress hook and the match_filter (cheap flag read): if set →
    `raise DownloadCancelled("user cancel")` (import from `yt_dlp.utils`; verify
    the exact class in the pinned version — fallback: any exception raised there
    aborts, catch OUR sentinel type in the caller).
  - between pipeline phases: after download / before tagging / before upload /
    between `_upload_tree` files / between artist-run releases.
- New `class UserCancelled(Exception)` in pipeline; the phase checks raise it; the
  yt-dlp abort path is translated into it. `_run`'s existing `except` chain gets an
  `except UserCancelled` branch BEFORE the generic error handler: phase
  `cancelled`, no error notification (`notify_download_error` must NOT fire for a
  user cancel), staging dir cleanup via the same finally-cleanup the error path
  uses (read how `_run` cleans up today and reuse).
- Artist run: check between releases in the fan-out loop; already-completed
  releases' delivered tracks stay recorded (`_record_delivered_safe` already ran).

### 3. UI

- `index.py` job cards (`_job_card`): pending → position badge + up/down + cancel
  icon-buttons; running → cancel button with confirm dialog
  (`t("jobs.cancel_confirm")`). Handlers call `jobs.cancel_job`/`reorder_job`;
  the existing `ui.timer` refresh picks up state changes.
- `history.py`: add `cancelled` to the phase filter options + badge color (neutral
  gray, not error red); detail dialog shows a cancel button while the job is
  non-terminal (look up live `JobState` by id).

### 4. Sync/scheduler interaction

`start_sync` flows through the same queue (it already shares the pool). On cancel of
a sync job: subscription `last_status = "idle"`, `last_error = None` — read
`_run_sync`'s status-write block and add the cancelled branch there.

## Testing

- Queue: fake executor (synchronous or controllable) — dispatch respects the cap;
  completion triggers dispatch of the next; FIFO order; reorder changes start
  order; cancel-pending removes + persists `cancelled`.
- Cancel token: monkeypatched `run_download` asserting the callable arrives and,
  when it returns True at a phase boundary, `UserCancelled` propagates to a
  `cancelled` history row and no error notification is sent (spy on `_notify_safe`).
- Match-filter/hook abort: unit-test the hook wrapper raises on flag set (no real
  download).
- Regression: existing `tests/test_jobs.py` behaviors (persist, notify on error,
  summary) unchanged.

## Definition of done

Acceptance criteria pass; manual verification: start a real 3-album artist run,
cancel mid-second-album → first album delivered + indexed, staging clean, history
`cancelled`; queue two jobs with concurrency 1 and reorder them; suite green;
version bumped; PR.
