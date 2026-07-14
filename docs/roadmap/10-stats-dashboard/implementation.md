# 10 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/stats.py` | **new** — pure query/aggregation helpers |
| `app/pages/stats.py` | **new** — `stats_content()` tiles + charts |
| `app/main.py`, `app/theme.py` | route + nav entry |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_stats.py` (new) | helper tests against seeded in-memory DB |

## Step plan

### 1. `app/stats.py` — aggregation helpers (UI-free, all `user_id`-scoped)

```python
def library_totals(user_id) -> LibraryTotals          # tracks/artists/albums/playlists
def downloads_per_month(user_id, months=12) -> list[tuple[str, int]]
def genre_distribution(user_id, top=8) -> list[tuple[str, int]]   # + "other" bucket
def mode_distribution(user_id) -> dict[str, int]
def format_distribution(user_id) -> dict[str, int]
def success_rate(user_id) -> tuple[int, int, int]      # done / error / partial
def top_artists(user_id, limit=10) -> list[tuple[str, int]]
def sync_overview(user_id) -> SyncOverview             # subs, status counts, new-track trend
```

- Library grouping reuses feature 03's `split_rel_path` / `is_playlist_folder`
  helpers from `library_index.py` if merged; otherwise a local single-pass grouping
  over `ServerTrack.rel_path` (and leave a note to consolidate when 03 lands).
- `DownloadHistory` aggregations: plain SQLModel `select` + `func.count` group-bys;
  month bucketing via `strftime('%Y-%m', created_at)` (SQLite) — acceptable
  SQLite-coupling, the app is SQLite-only by design.
- Exclude sync-internal rows? No — syncs ARE downloads; but add a `mode`-based
  breakdown so the chart explains itself.

### 2. Page (`app/pages/stats.py`)

- Layout: responsive grid — one row of stat tiles (`ui.card` + big number + caption,
  glass style like existing cards), then 2×2 chart grid collapsing to single column
  on mobile (Tailwind grid classes, pattern from the index page's destination cards).
- Charts with `ui.echart({...})`; set explicit `textStyle`/axis colors suited to the
  dark theme; keep one shared small helper `_chart_base(title)` for common options
  so the four charts stay consistent.
- Data loading in the content builder via `run.io_bound` once (no auto-refresh
  timer needed; add a manual refresh button).

### 3. Routing/nav/i18n

As in features 03/04: register in `main.py` sub-pages, nav link in `frame()`
(`app/theme.py`), keys in both languages (`stats.title`, tile captions, chart
titles, empty-state texts).

## Testing (`tests/test_stats.py`)

Seed an in-memory DB (fixture pattern from `tests/test_history.py`):

- totals with mixed artist/playlist rel_paths;
- month bucketing across a year boundary;
- genre top-N + "other" summation;
- success/partial classification (`phase`, `failed_tracks`);
- empty DB → zeros, no exceptions.

## Definition of done

Acceptance criteria pass; visual check on the dev server (dark theme legibility);
suite green; version bumped; PR.
