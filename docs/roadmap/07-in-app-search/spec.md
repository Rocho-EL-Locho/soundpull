# 07 — In-app YouTube Music search

**Phase:** 3 — Grow · **Effort:** M · **Depends on:** — · **Issue:** #41

## Goal

Search YouTube Music **inside Soundpull**: type a query, see songs/albums/artists/
playlists with cover thumbnails, click one — the download form is pre-filled with the
right URL and mode. No more tab-hopping to music.youtube.com (the bookmarklet stays
for people who browse there anyway).

## Current state

The download page (`app/pages/index.py`) only accepts a pasted URL; discovery happens
entirely outside the app. There is no search capability anywhere.

## Technology choice

Use **`ytmusicapi`** (unauthenticated) — the de-facto library for YouTube Music's
InnerTube API:

- no API key or login required for public search
- returns typed results (songs / albums / artists / playlists) with thumbnails and
  the ids needed to build canonical URLs
- yt-dlp's `ytsearchN:` alternative only searches plain YouTube videos — no albums,
  no artists → not good enough for this feature

**Risk & mitigation:** it is an unofficial API and can break with YT changes. Pin the
version in `pyproject.toml`; make every search call fail soft (error toast, download
form untouched) — a broken search must never affect downloads. The dependency is
import-isolated in one module so removal/replacement stays cheap.

## Scope

**In:**

- `app/search.py`: `search_music(query, limit)` returning normalized results
  (`type`, `title`, `artist`, `url`, `thumbnail`, `year/subtitle` where available),
  executed off the event loop.
- UI on the download page: a search row above the URL field (input + button, Enter
  submits), results as compact cards grouped by type; clicking a result fills the URL
  input, sets the mode toggle (song → `single`, album → `album`, playlist →
  `playlist`, artist → `artist`) and scrolls/focuses the download button.
- Result-to-URL mapping incl. album `browseId` → `OLAK5uy_…` audio-playlist URL
  resolution (an extra `get_album` call — only on click, not per result row).
- Debounce/limit: search fires on Enter/button only (no per-keystroke calls);
  results capped (e.g. 5 per type, "show more" per group optional).

**Out:**

- SoundCloud search (`scsearch:` yields track-only results; revisit after feature 06
  as a small follow-up).
- Search history, personalization, authenticated search (no cookies to ytmusicapi).
- A dedicated search page — it lives on the download page where the result is used.

## UX sketch

```
[ Search YouTube Music…            ] [Search]

Songs                 Albums                Artists
♪ Archangel — Burial  ▣ Untrue — Burial     ◉ Burial
♪ …                   ▣ …                     …
   (click → URL + mode filled, ready to download)
```

## Acceptance criteria

1. Searching a known artist returns grouped results with thumbnails within a few
   seconds, without blocking the UI.
2. Clicking a **song** fills a `watch?v=` URL + mode `single`; an **album** fills a
   `playlist?list=OLAK5uy_…` URL + mode `album`; an **artist** fills the channel URL +
   mode `artist`; a **playlist** fills the playlist URL + mode `playlist`. Each is
   downloadable as-is from there (mode auto-suggestion from feature 02, if merged,
   must not fight the explicit selection).
3. ytmusicapi errors/timeouts show a translated error toast; the rest of the page
   keeps working; nothing is logged at error level with the full stack on every miss
   (info/debug is fine).
4. No search request is sent per keystroke.
5. Search respects the app's trust model: only fixed Google endpoints are contacted
   (no user-supplied URL → no new SSRF surface).
6. i18n complete (de + en); suite green; zero pipeline/tagging changes.
