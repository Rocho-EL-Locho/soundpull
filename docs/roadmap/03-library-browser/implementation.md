# 03 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/pages/library.py` | **new** — `library_content()` |
| `app/main.py` | register `/library` in the `ui.sub_pages` router |
| `app/theme.py` | nav link in `frame()` |
| `app/library_index.py` | grouping/query helpers; album-scoped `backfill_lyrics` param |
| `app/scheduler.py` | second due-check: library scan interval |
| `app/models.py` | `UserSettings.library_scan_interval_hours: int = Field(default=0)`, `UserSettings.last_library_scan_at: datetime | None`, optional `navidrome_base_url: str = ""` |
| `app/pages/settings.py` | scan-interval field next to the existing scan button |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_library_index.py`, `tests/test_scheduler.py`, `tests/test_library_page.py` (new) | see Testing |

## Data model decision

**No new table.** Artist/album/track structure is derived from `ServerTrack.rel_path`
segments at query time:

- `rel_path = "Artist/Album/01 - Title.mp3"` → artist = segment 0, album = segment 1.
- Playlist folders (`<name> [<id>]`) are recognized by the trailing ` [<id>]` pattern
  (same shape `_playlist_folder_name` in `app/pipeline.py` produces) → grouped under
  "Playlists".
- Tracks directly under a single folder (depth 1) or deeper nesting: group whatever
  the first segment is as "artist" and the second (if any) as album `"—"` fallback.

Add pure helpers in `library_index.py` (unit-testable without DB):

```python
def split_rel_path(rel_path: str) -> tuple[str, str, str]   # (artist, album, filename)
def is_playlist_folder(name: str) -> bool
def library_tree(user_id: int) -> LibraryTree               # loads all rows once, groups in Python
```

A full load of one user's rows is fine at realistic library sizes (tens of thousands
of rows); don't build SQL-side grouping prematurely.

## Step plan

1. **Query/grouping helpers** in `library_index.py` (above), plus
   `count_stats(user_id)` and a `search(user_id, q)` LIKE-filter on
   `artist_norm`/`title_norm`.
2. **Album-scoped backfill**: extend `backfill_lyrics(user_id, *, prefix: str | None
   = None)` to filter candidate files to `rel_path.startswith(prefix)` — the existing
   settings-page button passes `prefix=None` (unchanged).
3. **Page** `app/pages/library.py`:
   - Follow the structural pattern of `app/pages/history.py` (list + refresh +
     dialogs) and `subscriptions.py`.
   - State: selected artist / selected album in local vars; `@ui.refreshable`
     sub-sections for the three panes; search input filters the loaded tree.
   - Actions call `library_ops.trash_track` / a new `library_ops.trash_folder`
     (trivial addition in feature 01's module: move a whole folder into the trash,
     drop all index rows under it) via `run.io_bound`, then refresh.
   - Rescan button: same `run.io_bound(scan_webdav, …)` flow as
     `app/pages/settings.py` (`scan_server` handler ~L298) — extract that handler
     into a shared helper so both pages use one implementation and both update
     `last_library_scan_at`.
   - Navidrome deep link (optional): if `navidrome_base_url` set, album row gets a
     link `<base>/app/#/album?filter={"name":"<album>"}`-style search URL — keep it a
     dumb search link, no API coupling.
4. **Routing/nav**: register in `main.py` next to `/history`; nav entry in
   `theme.py frame()` between Downloads and History.
5. **Scheduled scan**: in `app/scheduler.py`, `_tick` currently enumerates due
   `PlaylistSubscription`s. Add: for each user with `library_scan_interval_hours > 0`
   and `last_library_scan_at` older than the interval, run `scan_webdav` via the same
   worker path used by the settings button (`run.io_bound` is UI-side — from the
   scheduler thread call it directly, it's already a blocking-safe function), then
   stamp `last_library_scan_at`. Mirror `_is_due`'s shape; guard errors per user
   (log + continue).
6. **Settings**: number input for the interval in the WebDAV card, saved by the
   existing `save()`.

## Testing

- `split_rel_path` / `is_playlist_folder` / tree grouping: pure table tests, incl.
  playlist folders, depth-1 files, fullwidth characters.
- Search filter helper against a seeded in-memory DB (pattern from
  `tests/test_history.py`'s query-builder tests).
- Scheduler due-calc for the scan interval (extend `tests/test_scheduler.py`, pure).
- Album-scoped backfill filters by prefix (extend `tests/test_library_index.py` with a
  fake client, asserting only matching rel_paths are touched).
- i18n key parity test keeps passing.

## Definition of done

Acceptance criteria pass; manual smoke: scan a real WebDAV library, browse, trash a
test track and restore it from the settings trash; suite green; version bumped; PR.
