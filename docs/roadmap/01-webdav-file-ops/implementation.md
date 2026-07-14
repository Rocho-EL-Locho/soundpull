# 01 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/webdav_util.py` | add client-level primitives + rel-path safety helper |
| `app/library_ops.py` | **new** — index-aware trash/move/restore/purge layer |
| `app/library_index.py` | small query helpers (lookup/delete/update row by `rel_path`) |
| `app/models.py` | `UserSettings.trash_retention_days: int = Field(default=30)` |
| `app/pages/settings.py` | retention field (+ optional trash list) in the WebDAV card |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_webdav_util.py`, `tests/test_library_ops.py` (new) | see Testing |

## Step plan

### 1. `app/webdav_util.py` — primitives

webdav4's `Client` already provides `remove`, `move`, `download_file`, `exists`,
`mkdir` — and the existing `_SafePathClient` subclass handles path encoding, so the
wrappers stay thin:

```python
def resolve_rel(rel: str) -> str:
    """Validate a library-relative POSIX path; reject abs paths and traversal."""
    # posixpath.normpath, then reject "" / "." / leading "/" / any ".." segment

def download_file(client, remote_path: str, local_path: Path) -> None: ...
def delete_path(client, remote_path: str) -> None: ...
def move_path(client, src: str, dst: str) -> None: ...   # create parent dirs of dst
def path_exists(client, remote_path: str) -> bool: ...
```

- `move_path` needs parent-dir creation on the destination — reuse the logic of
  `pipeline._ensure_remote_dir` (either import it or move that helper into
  `webdav_util.py` and re-export; moving it here is the cleaner cut, pipeline then
  imports from webdav_util — pure relocation, no behavior change).
- Wrap transient errors with a small bounded retry like `pipeline._upload_with_retry`
  (2/4/8s exponential); factor the retry decision helper out if it's reusable, don't
  duplicate the policy.

### 2. `app/library_ops.py` — index-aware layer

All functions take `user_id`, load `UserSettings` themselves (mirroring how
`library_index.scan_webdav` does it), build the client via `make_client`, and join
paths under `webdav_folder` after `resolve_rel`.

```python
TRASH_DIR = ".soundpull-trash"

def trash_rel(rel_path: str, today: date) -> str:
    return f"{TRASH_DIR}/{today.isoformat()}/{rel_path}"

def trash_track(user_id: int, rel_path: str) -> str: ...       # returns trash rel path
def restore_track(user_id: int, trash_rel_path: str) -> str: ...
def move_track(user_id: int, src_rel: str, dst_rel: str) -> None: ...
def list_trash(user_id: int) -> list[TrashEntry]: ...           # walk TRASH_DIR (depth 2+)
def purge_trash(user_id: int, *, force_all: bool = False) -> int: ...
```

- `trash_track` with `trash_retention_days == 0` → `delete_path` directly.
- Index sync: add tiny helpers in `library_index.py` —
  `remove_by_rel_path(user_id, rel_path)`, `update_rel_path(user_id, old, new)`,
  and reuse `record_tracks` for restore (derive key via the existing
  `_artist_title_from_path`).
- Purge cutoff comes from the **date-named folder** (`<TRASH_DIR>/<YYYY-MM-DD>/…`), so
  no per-file metadata is needed; purge deletes whole dated folders older than the
  retention window.
- Trash-dir invisibility to the scanner: `scan_webdav` already skips dirs starting
  with `.` (see `_SKIP_DIR_NAMES` handling in `library_index.py`) — add an explicit
  test, and also add `TRASH_DIR` to `_SKIP_DIR_NAMES` for self-documentation.
- Call `purge_trash` best-effort (log + swallow, `_record_delivered_safe`-style) at
  the end of `scan_webdav`'s settings-page handler and after each `trash_track`.

### 3. Settings UI

- `app/pages/settings.py`, WebDAV card (near the dedup toggle / scan button): a
  `ui.number` bound to `trash_retention_days`, persisted by the existing single
  `save()`.
- Optional expander: `list_trash` table with per-entry restore button and an
  "empty trash" button (`purge_trash(force_all=True)`), both via `run.io_bound`.

### 4. i18n

Keys like `settings.trash_retention`, `settings.trash_retention_hint`,
`settings.trash_title`, `settings.trash_restore`, `settings.trash_empty`,
`settings.trash_empty_done` — in **both** `de` and `en`.

## Testing

- `resolve_rel`: table test — accepts `a/b/c.mp3`, rejects `../x`, `/abs`, `a/../..`,
  `""`.
- `trash_rel` path construction (incl. fullwidth-safe segments passing through
  unchanged).
- `trash_track` / `restore_track` / `move_track` with a **fake client** (monkeypatched
  `make_client` returning a stub that records calls + an in-memory "filesystem") and
  the real SQLite test session: assert file moves, `ServerTrack` row
  removed/re-added/updated.
- Purge cutoff: dated folders older/newer than retention; `force_all`.
- Scan skips `.soundpull-trash` (extend `tests/test_library_index.py`).
- Existing `tests/test_webdav_util.py` still green (path encoding unaffected).

## Definition of done

Acceptance criteria in `spec.md` all pass; `.venv/bin/python -m pytest` green;
version bumped; PR references this doc.
