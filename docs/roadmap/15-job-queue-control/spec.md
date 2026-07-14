# 15 — Job queue control (cancel / reorder / priority)

**Phase:** 4 — Comfort · **Effort:** M · **Depends on:** — · **Issue:** —

## Goal

Give the user control over the download queue: **cancel** a queued or running job,
**reorder** queued jobs, and see queue positions. Today a mis-pasted artist URL means
watching a 40-album run grind through the worker pool with no way to stop it.

## Current state

- Jobs are submitted straight into a bounded `ThreadPoolExecutor`
  (`app/jobs.py`); the pool's internal FIFO queue is invisible and untouchable —
  no cancel, no reorder, no position info.
- `JobState` drives live job cards on the index page (`ui.timer` polling); history
  rows track terminal phases (`done` / `error`).
- yt-dlp supports cooperative cancellation: raising from a progress hook /
  match_filter aborts the run (`yt_dlp.utils.DownloadCancelled` — verify the exact
  exception against the pinned version).

## Design decision: app-managed queue

Reordering inside a `ThreadPoolExecutor` is not possible; the clean cut is an
**app-side pending queue**: jobs land in a `deque` managed by `jobs.py`, and only up
to `max_concurrent_downloads` are submitted to the pool at once (dispatch on submit +
on every job completion). Pending entries are then trivially cancelable (remove from
deque) and reorderable (move within deque). This changes queue *mechanics*, not job
*execution* — `_run`/`_run_artist`/`_run_sync` bodies stay as they are.

## Scope

**In:**

- App-managed pending queue with dispatcher (thread-safe; the scheduler and UI both
  enqueue).
- **Cancel queued job**: removed before start → terminal phase `cancelled` (new
  phase value; history page filter + i18n updated).
- **Cancel running job**: sets a cancel flag on `JobState`; the pipeline checks it
  in the progress hook and match_filter (raise to abort the current yt-dlp run) and
  between phases (before tagging, before upload, between artist-run releases —
  a fanned-out artist run stops after the current release). Partial staged files are
  cleaned up like an error path; nothing half-uploaded is left behind (uploads are
  per-file — finish the in-flight file, skip the rest).
- **Reorder**: up/down (or drag) on queued job cards; queue position badge
  ("Queued — #3").
- Cancel/reorder controls on the index page's job cards; cancel also from the
  history detail dialog for running jobs.
- Sync jobs (scheduler-enqueued) are cancelable like manual ones; the subscription
  records `last_status` accordingly (a user cancel is not an error — status `idle`,
  not `error`).

**Out:**

- Pausing/resuming a running download (yt-dlp has no clean pause; cancel + later
  re-run with dedup/archive achieves the same).
- Per-job priority levels beyond manual ordering.
- Persisting the pending queue across restarts (in-memory stays; a restart drops
  queued jobs — same as today; the history page's retry covers recovery).

## Acceptance criteria

1. Cancelling a queued job removes it before any download starts; history shows
   `cancelled`.
2. Cancelling a running album download stops within a few seconds (current fragment
   finishes), cleans the staging dir, uploads nothing further, and the history row
   ends `cancelled` with the timeline log noting the user cancel.
3. Cancelling a running artist run stops after the in-flight release; already
   delivered releases stay delivered and indexed (partial success, honestly
   reported in the summary).
4. Reordering two queued jobs changes their start order; positions display
   correctly as the queue drains.
5. `max_concurrent_downloads` is still respected exactly; the scheduler's sync jobs
   flow through the same queue without starvation (FIFO within same order).
6. No regression in normal completion, error handling, notifications, or history
   persistence (`_persist` paths untouched in shape).
7. i18n complete (de + en); suite green.
