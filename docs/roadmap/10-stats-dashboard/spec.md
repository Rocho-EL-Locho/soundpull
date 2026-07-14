# 10 — Statistics dashboard

**Phase:** 4 — Comfort · **Effort:** S · **Depends on:** — (nicer with 03) · **Issue:** —

## Goal

A read-only `/stats` page that turns data the app already stores into insight and a
bit of delight: how big the library is, how it grows, what gets downloaded, and how
healthy the machinery runs.

## Data sources (all existing — no new collection)

- `ServerTrack` → library size: tracks, distinct artists, albums (via `rel_path`
  grouping, same helpers as feature 03), playlist-folder count.
- `DownloadHistory` → downloads over time (`created_at`), mode distribution
  (album/single/playlist/artist), genre distribution, audio-format distribution,
  failure/partial rate (`phase == "error"`, `failed_tracks`), busiest artists
  (`artist` column).
- `PlaylistSubscription` (+ `ArtistSubscription` once 08 lands) → sync activity:
  last-status overview, new-tracks-per-sync trend (`last_new_count`).

## Scope

**In:**

- Stat tiles: total tracks / artists / albums / playlists; downloads this month;
  success rate.
- Charts (NiceGUI ships `ui.echart` — no new dependency):
  - downloads per week/month (bar or line, last 12 months)
  - genre distribution (donut, top 8 + "other")
  - mode + format distribution (small bars)
  - top 10 artists by downloads (horizontal bar)
- Per-user only (all queries filtered by `user_id`, like every other page).
- Empty-state handling (fresh install → friendly hints instead of empty charts).
- Nav entry; glass-theme styling consistent with the rest of the app; charts must
  be readable on the dark theme (explicit echarts text/axis colors).

**Out:**

- Listening stats (that's Navidrome's data, not Soundpull's).
- New tracking/telemetry of any kind; no new columns or tables.
- Export/reporting.

## Acceptance criteria

1. `/stats` renders correct numbers for a seeded DB (verified against handwritten
   SQL in tests for the query helpers).
2. All aggregation happens in query helpers that are unit-tested without the UI.
3. Page loads without blocking (heavy aggregation via `run.io_bound` if needed —
   at realistic data sizes plain queries are fine; measure, don't assume).
4. Charts are legible in the dark glass theme; no hard-coded user-facing strings
   (i18n de + en, including chart labels).
5. Suite green; zero pipeline/tagging/model changes.
