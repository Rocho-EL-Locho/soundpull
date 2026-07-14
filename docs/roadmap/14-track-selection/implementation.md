# 14 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/pipeline.py` | `run_download(…, playlist_items: str | None = None)` → opts injection; preview helper reusing `enumerate_playlist_tracks` |
| `app/jobs.py` | thread `playlist_items` from `start_job` into `_run` |
| `app/pages/index.py` | Preview button + selection dialog |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_pipeline.py`, `tests/test_jobs.py` | see Testing |

## Step plan

### 1. Pipeline (`app/pipeline.py`)

- `run_download` gains `playlist_items: str | None = None`. When set (and the run
  is multi-track), set `ydl_opts["playlist_items"] = playlist_items` in the same
  opts-level block where `download_archive` / socket timeout are applied — **never**
  by editing the flag lists. `None` → no key set → byte-identical opts (assert in
  the snapshot test).
- Verify the interaction with `_download_with_retries`: `expected_ids` comes from
  the match_filter's `on_seen`, which only sees entries yt-dlp actually visits —
  with `playlist_items` set, unselected entries are never visited, so expected/
  finished accounting works out naturally. Confirm by reading `on_seen`, then cover
  with a test.
- Preview helper: `preview_tracks(url, mode, on_server) ->
  list[PreviewTrack(index, title, artist, duration, on_server)]` — thin wrapper
  around `enumerate_playlist_tracks` + `track_key` membership against the loaded
  index dict (same normalization the match_filter uses — reuse, don't re-derive).
- Confirm (during implementation, against the pinned yt-dlp) that `playlist_index`
  in output templates keeps the ORIGINAL numbering under `playlist_items` — the
  playlist filename template `_PLAYLIST_TRACK_TMPL` depends on it. If it renumbers,
  fall back to rejecting selection for playlist mode in v1 (album mode has no index
  prefix issue) and note it in the spec.

### 2. Jobs (`app/jobs.py`)

`start_job(…, playlist_items=None)` → stored on `JobState` (for the history log
line "N of M selected") → passed to `run_download` in `_run`. Sync jobs
(`_run_sync`) never set it.

### 3. UI (`app/pages/index.py`)

- Preview `ui.button` beside download, enabled only for `album`/`playlist` modes
  with a non-empty URL.
- Handler: `tracks = await run.io_bound(preview_tracks, …)` (pass the on_server
  dict only when dedup toggle + WebDAV, mirroring `start_job`'s condition — read how
  `index.py` builds that today and reuse) → `ui.dialog` with a checkbox list
  (virtualized via `ui.scroll_area` for big playlists), select-all/none, live
  count on the confirm button.
- Confirm → build the `playlist_items` spec from the CHECKED original indices —
  compress consecutive runs to ranges (`1-4,6,9-11`); pure helper
  `indices_to_spec(list[int]) -> str` in `pipeline.py` or `matching`-adjacent
  neutral module.
- Dialog state is per-open (no persistence); closing without confirm changes
  nothing.

## Testing

- `indices_to_spec`: singles, ranges, unsorted input, empty (→ raises — the UI
  prevents zero-selection).
- Opts injection: `playlist_items=None` → key absent (parity assert);
  `"1-3"` → key present with exact value; flag-list snapshots untouched.
- `preview_tracks` with fake probe payload + seeded index: on_server flags correct.
- Jobs threading: `start_job(playlist_items=…)` reaches `run_download`
  (monkeypatched) — extend `tests/test_jobs.py`.

## Definition of done

Acceptance criteria pass; manual verification: real album with 2 deselected tracks
(count + tags + no stray files), real playlist with an on-server track (m3u
references it); suite green; version bumped; PR.
