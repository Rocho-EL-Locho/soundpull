# 12 — Batch import from track list

**Phase:** 3 — Grow · **Effort:** M · **Depends on:** 07 · **Issue:** —

## Goal

Paste a plain list of tracks — one `Artist - Title` per line (or simple CSV) — and
Soundpull matches each entry against YouTube Music, shows the matches for review,
and downloads the confirmed ones. This is the generic "get many known songs into the
library" tool, and it doubles as the **match engine** that feature 13 (Spotify/Apple
import) feeds with parsed playlists.

## Current state

Only single-URL downloads exist. Feature 07 provides `search_music()` (ytmusicapi) —
the building block this feature composes into a pipeline: parse → match → review →
enqueue.

## Flow

```
[textarea: paste list]  →  Parse (client-side preview of recognized lines)
        →  Match (background, progress)   →  Review table:
              input line | matched song (cover, artist–title) | confidence | ☑
              already-in-library entries pre-unchecked + badged
        →  [Download selected]  →  one batch job, tracks download as singles
```

- **Parsing**: accepted line shapes: `Artist - Title`, `Artist – Title` (en dash),
  `Title - Artist` is NOT guessed (ambiguous — document), CSV with `artist,title`
  header, tab-separated. Unparseable lines are listed as skipped, never silently
  dropped.
- **Matching**: per line one `search_music(…, filter=songs)` call; confidence =
  normalized similarity of artist+title (stdlib `difflib`) between input and result.
  High confidence (≥ 0.85) pre-checked; medium shows top-3 alternatives in a
  dropdown; no result → row marked unmatched.
- **Dedup**: entries whose `track_key` is already in the `ServerTrack` index are
  badged "already in library" and pre-unchecked (WebDAV destination only, like all
  index features).
- **Download**: confirmed rows become ONE batch job (single job card + one
  `DownloadHistory` row with `total_tracks = N`), internally fanning out
  single-mode downloads — the artist-run orchestrator pattern, not N separate jobs
  (a 100-line list must not create 100 history rows).

## Scope

**In:** the flow above; list size cap (default 200 lines, hard limit); rate-limited
matching (search calls sequential or small pool — don't hammer ytmusicapi); i18n.

**Out:**

- Fuzzy audio matching, ISRC lookups.
- Album-level import lines ("Artist - Album") — v1 is tracks only (albums are one
  URL paste away already).
- Auto-download without the review step.

## Acceptance criteria

1. A pasted 10-line list yields a review table with correct matches, confidence
   sorted, library-duplicates pre-unchecked.
2. Unparseable and unmatched lines are visibly reported, never silently dropped.
3. Downloading N selected rows produces one job / one history row ("k von N" on
   partial failure, reusing the existing partial-delivery surfacing) and N tagged
   singles delivered to the configured destination.
4. Matching a 200-line list neither blocks the UI nor trips API abuse (bounded
   concurrency, progress visible, cancelable by leaving the page — matching holds no
   locks).
5. The match engine is importable as a module function (feature 13 consumes it
   without the UI).
6. i18n complete (de + en); suite green; no pipeline/tagging changes.
