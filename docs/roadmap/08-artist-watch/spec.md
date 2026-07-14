# 08 — Artist watch & discography gaps

**Phase:** 3 — Grow · **Effort:** M · **Depends on:** — (pairs well with 03/06) · **Issue:** —

## Goal

Two complementary ways to keep a collection **complete per artist**:

1. **Artist watch** — subscribe to an artist; Soundpull periodically re-enumerates
   their releases and auto-downloads what's new (the dedup index makes this
   incremental for free), notifying via the existing channels.
2. **Discography gaps** — a one-shot view: enumerate an artist's releases, compare
   against the library, show what's missing, cherry-pick and download.

This is the artist-level analogue of the existing playlist interval-sync (#21) and
reuses almost all of its machinery.

## Current state

- One-shot **artist runs** exist (`run_artist_download` + `enumerate_artist` in
  `app/pipeline.py`, with compilation filtering, `own_artist` crediting, staging
  dedup) — but only manually triggered from the download page.
- **Playlist** subscriptions exist (`PlaylistSubscription` model,
  `app/scheduler.py` tick, `jobs.start_sync`/`_run_sync`) with per-sub interval,
  status, error state and `notify_new_tracks`/`notify_sync_error` notifications.
- Artist runs already default dedup ON for WebDAV — a re-run only pulls new tracks.
  Artist watch is conceptually "scheduled artist re-run".

## Scope

**In:**

- New `ArtistSubscription` model mirroring `PlaylistSubscription`: channel/profile
  URL, display name, `interval_hours` (default **168** — enumeration is expensive,
  weekly is the sensible default), enabled flag, genre, audio format, `last_*`
  status fields.
- Scheduler integration: due artist subs enqueue an **artist sync job** = the
  existing artist-run path with dedup forced ON, WebDAV-only (like playlist sync),
  plus subscription status updates and notifications (`notify_new_tracks` /
  `notify_sync_error` semantics reused; new-track count aggregated across releases).
- UI: the subscriptions page gets two sections/tabs — **Playlists** (existing) and
  **Artists** (new): add by URL (validated as artist-shaped for the source), interval
  select, enable toggle, "sync now", status/last-result display. Mirror the existing
  playlist card layout.
- **Discography gaps**: on an artist subscription card (and as a standalone "check an
  artist" input on the same page): enumerate releases → compare release titles vs
  the library's album folders for that artist (from `ServerTrack.rel_path`) → list
  missing releases with checkboxes → download selected as normal artist-mode album
  jobs. Comparison is **folder/title-level** (cheap); the docs/UI must be honest that
  it's approximate (casing/renamed folders can produce false "missing" entries —
  the dedup filter then makes downloading them a cheap no-op anyway).

**Out:**

- Per-release selection *rules* (e.g. "albums only, no singles") — first iteration
  watches everything `enumerate_artist` yields (it already filters third-party
  compilations).
- Release-date awareness / "only releases after subscription date" (dedup makes this
  unnecessary: existing tracks are skipped; the *initial* sync equals a full
  discography download — the UI must say this clearly when adding a watch, and offer
  the same `initial_mode` choice as playlist subs: **download all** vs **mark
  existing** — mark-existing = run enumeration once and record all current releases
  as seen without downloading).
- Non-WebDAV destinations (sync is WebDAV-only, like playlist sync).

## Acceptance criteria

1. Adding an artist watch with `initial_mode = mark_existing` records the current
   discography without downloading; a later release (simulated) is downloaded on the
   next due tick and a `notify_new_tracks` notification fires with the correct count.
2. `initial_mode = download_all` behaves like a manual artist run with dedup ON.
3. A failing sync sets `last_status = error` + `last_error` and fires
   `notify_sync_error` (if enabled), without affecting other subscriptions.
4. Gap view lists releases missing from the library; downloading a selected release
   files it under the correct `own_artist` folder; already-complete artists show an
   "all present" state.
5. Scheduler: artist and playlist subs coexist; intervals independent; disabled subs
   never run.
6. Enumeration cost is bounded: one enumeration per due sub per tick; "sync now"
   ignores the interval but respects the running-job dedup (no double-enqueue while
   a sync for the same sub is active).
7. i18n complete (de + en); suite green; no pipeline/tagging changes beyond wiring.
