# 03 — Library browser

**Phase:** 2 — Manage · **Effort:** L · **Depends on:** 01 · **Issue:** —

## Goal

A `/library` page that makes the (currently invisible) server index browsable:
**artists → albums → tracks**, with search, counts, and per-item actions. Plus an
optional **scheduled library scan** so the index stays fresh without pressing the
settings-page button.

This is the "see what you have" pillar of collection management, and the natural
home for the duplicate finder (04) and health check (05) entry points later.

## Current state

- The `ServerTrack` index (per user: `artist_norm`, `title_norm`, `rel_path`) exists
  and is populated by `scan_webdav` (`app/library_index.py`) — but the only trigger is
  a **manual button** on the settings page, and the only consumers are the download
  dedup and m3u references. Nothing displays the library.
- Display names are recoverable from `rel_path` segments (layout
  `<Artist>/<Album>/<file>.mp3`; playlist folders are `<name> [<id>]/…`).

## Scope

**In:**

- New page `/library` (nav entry in the app shell):
  - **Artists view**: searchable list with track counts; playlist folders grouped
    into a separate "Playlists" section (they are not artists).
  - Drill-down: artist → albums (folder-level grouping) → track list.
  - Global search box (matches artist and title, against the `*_norm` columns).
  - Header stats: total artists / albums / tracks; last-scan time; a **Rescan** button
    (same handler as the settings page, moved/shared).
- Per-item actions (wired to feature 01):
  - Track: **delete (to trash)**.
  - Album: **delete folder (to trash)**, **backfill lyrics for this album**
    (album-scoped variant of the existing `backfill_lyrics`).
  - Optional: "Open in Navidrome" deep link if a new optional setting
    `navidrome_base_url` is configured (plain link, no API).
- **Scheduled scan**: `UserSettings.library_scan_interval_hours` (0 = off, default 0)
  — when set, the existing scheduler tick also enqueues a scan when due.
- Everything read-heavy runs via `run.io_bound`; big lists paginate or lazy-render.

**Out:**

- Audio streaming/preview (that is Navidrome's job).
- Tag editing (feature 05 covers metadata *fixes*).
- Re-download of a library item (no source URL is stored for scanned files; feature 07
  later gives "search this track" as the natural path — do not build a half-solution
  here).

## UX sketch

```
Library                                    [Search…] [Rescan]
1.204 tracks · 87 artists · 143 albums · scanned 2h ago

Artists                        BCee (3 albums, 34 tracks)
  A.M.C          12 ▸           ▸ Come & Find Me (12)
  BCee           34 ▸           ▸ Northpoint (11)
  Bop            18 ▸           ▸ Best of BCee (11)
Playlists                        [tracks of selected album,
  DnB Mix [PL…]  25 ▸            per-track trash action]
```

Glass-theme cards like the existing pages; mobile-friendly drill-down (one column at a
time on small screens).

## Acceptance criteria

1. `/library` renders the full index grouped correctly (artists vs playlist folders),
   with accurate counts and working search.
2. Track/album delete moves files to trash (01), updates the index, and the view
   refreshes without a full page reload.
3. Album-scoped lyrics backfill only touches that album's folder.
4. With `library_scan_interval_hours > 0`, the scheduler triggers a scan when due;
   `= 0` means never (default — no behavior change for existing users).
5. A library with 0 tracks shows a friendly empty state pointing to the scan button /
   download page.
6. All strings via `t()` (de + en); nav entry present in `frame()`.
7. Full test suite green; no pipeline/tagging changes.
