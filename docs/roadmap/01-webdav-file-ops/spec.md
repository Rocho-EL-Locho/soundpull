# 01 — WebDAV file operations + trash safety net

**Phase:** 1 — Foundation · **Effort:** M · **Depends on:** — · **Issue:** —

## Goal

Give Soundpull safe primitives to **modify** the existing remote library: download a
remote file to local staging, delete, move/rename — plus a **trash** safety net so
nothing a user (or a later automated feature) removes is immediately gone.

This is the foundation for the whole "manage" phase: duplicate cleanup (04), library
browser actions (03), and health fixes (05) all need these operations.

## Current state

- `app/webdav_util.py` only creates clients (`make_client`, with SSRF allowlist +
  `_SafePathClient` path encoding) and lists directories (`list_dirs`).
- Uploads live in `app/pipeline.py` (`_upload_with_retry`, `_upload_tree`,
  `_ensure_remote_dir`) directly on the webdav4 client; `app/library_index.py`
  uploads `.lrc` sidecars via `client.upload_fileobj`.
- **No delete / move / download-to-local exists anywhere.** Index pruning
  (`_prune_missing`) only removes DB rows, never remote files.

## Scope

**In:**

- Client-level primitives in `webdav_util.py`: `download_file`, `delete_path`,
  `move_path`, `path_exists` (thin wrappers over webdav4 with retry + path safety).
- An **index-aware operations layer** (new `app/library_ops.py`):
  - `trash_track(user_id, rel_path)` — move the file into
    `.soundpull-trash/<YYYY-MM-DD>/<original rel_path>` under the user's
    `webdav_folder`, delete its `ServerTrack` row.
  - `restore_track(user_id, trash_rel_path)` — move back, re-record in the index.
  - `move_track(user_id, src_rel, dst_rel)` — move + update `ServerTrack.rel_path`.
  - `list_trash(user_id)` / `purge_trash(user_id)` — enumerate and hard-delete
    entries older than the retention window.
- New setting `UserSettings.trash_retention_days` (int, default `30`; `0` = delete
  immediately without trash). Settings-page field in the WebDAV section.
- Trash purge runs opportunistically (e.g. after each successful `scan_webdav` or
  trash operation) — no new scheduler machinery required.
- Path safety: every operation resolves relative to the user's `webdav_folder` and
  **rejects** absolute paths and `..` traversal.

**Out:**

- Any standalone UI beyond the settings field (a small "Trash" list with restore/purge
  buttons on the settings page is a nice-to-have — include it if cheap; features 03/04
  bring the real UI).
- Touching local library files (`LOCAL_MUSIC_ROOT` stays staging-only).
- Rewriting playlist `.m3u8` references of deleted files (handled in feature 04, where
  deletions actually happen at scale).

## UX

- Settings → WebDAV section gains: trash retention number input (with a hint that `0`
  disables the trash), and — if the nice-to-have is included — a "Trash" expander
  listing trashed files with restore / empty-trash actions.
- All new strings via `t()` in both languages.

## Acceptance criteria

1. Deleting a track via `trash_track` moves the file into the dated trash folder,
   removes its `ServerTrack` row, and a subsequent `scan_webdav` does **not**
   re-index it (trash folder starts with `.` → already skipped by the scan's
   dot-prefix rule — verify with a test).
2. `restore_track` puts the file back at its original path and the index knows it again.
3. `purge_trash` hard-deletes only entries older than `trash_retention_days`.
4. With `trash_retention_days = 0`, delete is immediate (no trash entry).
5. Path traversal attempts (`../x`, absolute paths, empty segments) raise before any
   network call.
6. Operations never leave the user's `webdav_folder` base.
7. Full test suite green; no change to download/tag output (this feature is entirely
   outside the pipeline).
