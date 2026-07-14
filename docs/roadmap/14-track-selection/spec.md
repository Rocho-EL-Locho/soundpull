# 14 — Track selection before download

**Phase:** 4 — Comfort · **Effort:** M · **Depends on:** — · **Issue:** —

## Goal

For album and playlist URLs, show the track list **before** downloading and let the
user deselect tracks. Today every download is all-or-nothing; skipping the two skits
on an album or the three known songs in a 200-track playlist requires manual cleanup
afterwards.

## Current state

- `enumerate_playlist_tracks` (`app/pipeline.py`) already probes a playlist/album
  flat and returns per-entry metadata — the preview data source exists.
- yt-dlp natively supports partial downloads via the `playlist_items` option
  (an index spec like `"1,3,5-7"`) — an **opts-level** setting, exactly the
  parity-safe layer where `download_archive`/`socket_timeout` already live. The flag
  lists and tag chain are untouched by it.
- The dedup match_filter already marks which entries are on the server — the preview
  can reuse that knowledge to pre-uncheck them.

## Scope

**In:**

- A **Preview** button next to the download button, active for `album` and
  `playlist` modes: probes the URL (`run.io_bound`), opens a dialog listing tracks
  (index, title, artist, duration when available), all pre-checked.
- Already-on-server tracks (when the dedup toggle is on and destination is WebDAV)
  are badged and pre-unchecked — the preview makes the invisible skip visible.
- Deselection state → `playlist_items` spec passed into `run_download` → injected
  into the yt-dlp opts for the main download (multi-track paths only).
- Playlist mode nuance: the `.m3u8` and the `%(playlist_index)04d` filename prefix
  keep the ORIGINAL indices (yt-dlp preserves entry indices with `playlist_items` —
  verify; the m3u simply lists fewer files). Document the resulting gaps in the
  numbering as intended.
- Starting the download **without** pressing Preview behaves exactly as today
  (preview is optional, no extra probe cost on the default path).

**Out:**

- Artist mode (release-level selection belongs to the discography gap view, 08).
- Single mode (nothing to select).
- Persisting selections (one-shot per download).
- Range/size-based selection UI (checkboxes suffice; "select none/all" buttons yes).

## UX sketch

```
[URL …………………………………]  (album)     [Preview] [Download]

┌ Preview: "Untrue" — Burial (13 tracks, 2 already in library) ┐
│ [Select all] [Select none]                                   │
│ ☑ 01 Untitled          ☑ 05 Archangel                        │
│ ☐ 02 Archangel  ⬤ in library                                 │
│ …                                                            │
│                       [Download 11 tracks]                   │
└──────────────────────────────────────────────────────────────┘
```

## Acceptance criteria

1. Preview on an album URL lists all tracks with correct order/titles; confirming
   with 2 tracks deselected downloads exactly N−2 tracks, tagged as usual.
2. A playlist preview with on-server tracks shows them badged/pre-unchecked; the
   resulting m3u still references the on-server copies (existing reference
   mechanism unchanged).
3. Downloading without preview is byte-identical to today's behavior (no
   `playlist_items` in the opts — assert in tests).
4. The expected/finished accounting (`expected_ids`/`failed_tracks`, partial-delivery
   warning) is correct under a partial selection.
5. Preview errors (region-locked, dead URL) show a translated message; the download
   button still works independently.
6. i18n complete (de + en); parity snapshots unchanged; suite green.
