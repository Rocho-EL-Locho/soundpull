# 04 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/duplicates.py` | **new** — analysis, grouping, keeper heuristic, cleanup, m3u repair |
| `app/library_index.py` | factor the WebDAV walk into a reusable iterator |
| `app/models.py` | **new table** `DuplicateReport` (JSON blob per user) |
| `app/pages/duplicates.py` | **new** — `duplicates_content()` review UI |
| `app/main.py`, `app/theme.py` | route + nav entry |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_duplicates.py` (new) | see Testing |

## Step plan

### 1. Refactor the walk (`library_index.py`)

`scan_webdav` owns the traversal (depth-limited, `_SKIP_DIR_NAMES`, error tracking).
Extract a generator both callers share:

```python
def iter_library_files(client, base: str, *, max_depth: int = 8
                       ) -> Iterator[tuple[str, list[str]]]:
    """Yield (rel_path, errors) for every audio file under base."""
```

`scan_webdav` keeps its exact semantics (record + prune + error handling) on top of
it — its tests must stay green unchanged. This is a pure refactor; do it as the first
commit.

### 2. Analysis (`app/duplicates.py`)

```python
def analyze(user_id: int, progress: Callable[[str], None] | None = None) -> Report:
    # walk → for each file: key = track_key(*_artist_title_from_path(rel))
    # exact:    dict[key, list[PathInfo]] where len > 1
    # probable: second pass over the *remaining* singles:
    #           noise_key = (artist_norm, strip_noise(title_norm))
    #           group singles whose noise_key collides
```

- `PathInfo`: `rel_path`, `folder`, `folder_track_count` (count files per folder in
  the same walk — one pass, no extra requests), `is_playlist_folder`.
- `strip_noise`: adapt the regex set of `pipeline._strip_title_noise` — **import and
  reuse** the pipeline helper if it is importable as a pure function (it is; it's
  used at staging time), do not fork the patterns. If its signature is awkward, wrap
  it, don't copy it.
- Keeper suggestion: `max(paths, key=(not is_playlist_folder, folder_track_count,
  -len(rel_path)))` — biggest real-album folder, artist tree over playlist folder,
  then shortest path.
- Serialize the report to JSON (groups, tier, suggestion, timestamps) into
  `DuplicateReport` (one row per user, replaced on re-run — precedent for JSON
  columns: `PlaylistSubscription.playlist_files`).

### 3. Report table (`app/models.py`)

```python
class DuplicateReport(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True)
    created_at: datetime = Field(default=None)   # scalar-default rule: use sa CURRENT_TIMESTAMP fallback
    groups: str = Field(default="[]")            # JSON
```

Remember: **no `from __future__ import annotations` in models.py**; additive table →
auto-created by `init_db()`.

### 4. Cleanup + m3u repair

```python
def resolve_group(user_id: int, group: Group, keeper: str) -> ResolveResult:
    # 1. for each non-keeper: library_ops.trash_track(user_id, rel_path)
    # 2. ensure index row points at keeper (update_rel_path / record_tracks)
    # 3. repair_playlist_refs(user_id, removed={rel_paths}, keeper=keeper)
```

`repair_playlist_refs`:

- Find candidate playlists: walk playlist folders (`<name> [<id>]/`) for `.m3u8`
  files **and** all `PlaylistSubscription.playlist_files` manifests.
- An m3u line is either a bare filename (in-folder track) or a cross-folder relative
  path — resolve each line against the playlist folder
  (`posixpath.normpath(posixpath.join(folder, line))`) and compare with the removed
  rel_paths.
- Matching line → replace with `posixpath.relpath(keeper, folder)` (exactly the frame
  `_build_playlist_manifest` / the m3u writer in `app/pipeline.py` uses — read
  `_write_m3u` first and mirror it: UTF-8, newline handling).
- Download-edit-upload the m3u via feature 01 primitives; update the subscription
  manifest JSON in the same transaction.
- Keep this a **pure function over strings** at its core
  (`rewrite_m3u(text, folder, removed, keeper) -> str | None`) so it is trivially
  testable; the network wrapper stays thin.

### 5. Background execution + UI

- Analysis runs minutes on big libraries. Use a module-level in-memory state
  (`_analysis_state[user_id] = {"phase", "progress", "report_id"}`) + a worker thread
  via the shared pattern: submit with `run.io_bound` from the page handler and poll
  with `ui.timer` — same live-progress idea as the job cards on the index page, but
  **no** `DownloadHistory` row (this is maintenance, not a download).
- Page `app/pages/duplicates.py`: header (analyze button, last-report info), exact
  groups as cards with radio keeper selection (`ui.radio`), per-group confirm dialog
  listing exactly what will be trashed and which playlists get re-pointed; probable
  tier collapsed by default; bulk button for exact tier only.
- Refresh pattern per `history.py` (`@ui.refreshable` list + reload after actions).

## Testing (`tests/test_duplicates.py`)

- Grouping: synthetic walk data → exact groups found; singles untouched; probable
  tier only from noise-stripped collisions; feat-variant collapse covered by
  existing `track_key` (add one regression case).
- Keeper heuristic table: album-vs-single, playlist-folder demotion, tie-breaks.
- `rewrite_m3u`: bare-filename line, `../Artist/Album/x.mp3` line, untouched lines,
  playlist that loses nothing → returns `None` (no upload).
- `resolve_group` with fake client + real test DB: files moved to trash, index row
  correct, manifest JSON updated.
- Walk refactor: `scan_webdav` behavior unchanged (existing tests must not change).

## Definition of done

Acceptance criteria pass; manual smoke on a real library copy (seed a duplicate,
analyze, resolve, verify in Navidrome that the playlist still plays); suite green;
version bumped; PR.
