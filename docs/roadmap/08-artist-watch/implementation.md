# 08 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names. **Read
`jobs._run_sync` / `start_sync` and `scheduler.py` first** — this feature is a
structural mirror of the playlist-sync path.

## Touch points

| File | Change |
|---|---|
| `app/models.py` | **new table** `ArtistSubscription` |
| `app/jobs.py` | `start_artist_sync` / `_run_artist_sync` (+ config snapshot) |
| `app/scheduler.py` | tick also enqueues due artist subs |
| `app/pipeline.py` | none expected (reuse `enumerate_artist`, `run_artist_download`) |
| `app/pages/subscriptions.py` | tabs Playlists / Artists; artist cards; gap view |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_artist_watch.py` (new), `tests/test_scheduler.py` | see Testing |

## Step plan

### 1. Model (`app/models.py`)

Mirror `PlaylistSubscription` field-for-field where it applies:

```python
class ArtistSubscription(SQLModel, table=True):
    id, user_id, url, name
    interval_hours: int = Field(default=168)
    enabled: bool = Field(default=True)
    genre: str; audio_format: str
    initial_mode: str = Field(default="mark_existing")   # download_all | mark_existing
    seen_releases: str = Field(default="[]")             # JSON list of release keys
    last_checked_at / last_synced_at / last_status / last_error / last_new_count
    created_at
```

`seen_releases` stores enumerated release identifiers (playlist id or normalized
title) — needed for `mark_existing` and to compute "new since last check" for the
notification count without diffing track level. Additive table → safe migration.

### 2. Jobs (`app/jobs.py`)

- `_ArtistSyncConfig` snapshot dataclass mirroring `_SyncConfig` (WebDAV target,
  cookie, tag options, lyrics flag, language for notifications — copy the loading
  code path of `start_sync`, factor shared parts into a helper instead of
  duplicating if it stays readable).
- `start_artist_sync(sub_id)`:
  - in-flight guard: skip if a job for this `sub_id` is queued/running (same
    mechanism `start_sync` uses — read it and reuse).
  - enqueue `_run_artist_sync` on the worker pool.
- `_run_artist_sync`:
  1. `enumerate_artist(url)` → releases.
  2. `new = [r for r in releases if key(r) not in seen_releases]`.
  3. `initial_mode == "mark_existing"` and first run (`last_synced_at is None`):
     record all as seen, download nothing, status ok.
  4. else: run the existing artist-run path for the **new** releases only — reuse
     `run_artist_download`'s internals; if it only accepts a full artist URL,
     extend it with an optional pre-enumerated `releases=` parameter (small,
     backwards-compatible: default `None` → enumerate itself). Dedup forced ON.
  5. persist seen keys, status fields, `last_new_count` (aggregate
     `new_track_count` from results).
  6. notifications via `_notify_safe` after the terminal state is persisted —
     same trigger semantics as `_run_sync` (`new_track_count > 0` → new-tracks
     event; exception path → sync-error event).
- History: give the sync run a `DownloadHistory` row like playlist syncs do (check
  how `_run_sync` persists — mirror it, `mode="artist"`).

### 3. Scheduler (`app/scheduler.py`)

`_tick` gains a second query: enabled `ArtistSubscription`s where `_is_due(
last_checked_at, interval_hours)` (the existing helper is table-agnostic — reuse
it) → `start_artist_sync(sub.id)`. Per-sub error guard (log + continue).

### 4. UI (`app/pages/subscriptions.py`)

- Wrap the existing content in `ui.tabs` (Playlists / Artists) — keep the playlist
  tab's code untouched apart from the wrapper.
- Artists tab: create form (URL + name auto-filled from probe, interval select
  with daily/weekly/monthly presets, genre/format selects, initial-mode radio with
  explanatory captions — reuse the playlist form's structure), cards with status
  line, enable toggle, "sync now", delete.
- URL validation: source must support artist mode (`detect_source(url)` +
  `supports_artist` once 02 is merged; before that, reuse the same YouTube host
  check the index page's artist mode implies).
- **Gap view**: button "Check discography" on a card (and a standalone URL input at
  the top of the tab): `run.io_bound(enumerate_artist)` → compare release names
  against album folders under that artist in the index
  (`split_rel_path` helper from feature 03 if merged; else derive inline from
  `rel_path`) using the same normalization as `track_key`'s `_norm` — dialog lists
  missing releases with checkboxes → enqueue selected as artist-mode album jobs
  (`start_job` per release URL with `own_artist` passed — check what
  `run_artist_download` passes per release and mirror it).

## Testing

- Due-calc for artist subs (extend `tests/test_scheduler.py` — pure).
- `_run_artist_sync` state machine with monkeypatched `enumerate_artist` +
  download runner: mark_existing first run; new-release detection; error path sets
  status + fires error notification (assert via monkeypatched notify).
- seen-release key normalization stability (same release enumerated twice → one key).
- Gap comparison: seeded index rows vs fake enumeration → correct missing set;
  casing differences documented behavior (normalized compare).
- In-flight guard: second `start_artist_sync` while first queued → no-op.

## Definition of done

Acceptance criteria pass; manual smoke with a small real artist (mark_existing, then
force one release out of `seen_releases` and "sync now" → only that release
downloads, notification arrives); suite green; version bumped; PR.
