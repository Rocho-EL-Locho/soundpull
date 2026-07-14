# 13 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names. **Requires 07 + 12
merged.** Read `pipeline._write_m3u`, `_build_playlist_manifest` and
`_playlist_folder_name` before the m3u milestone — the reference mechanics exist.

## Touch points

| File | Change |
|---|---|
| `app/playlist_import.py` | **new** — URL detection, Spotify + Apple parsers |
| `app/config.py` | `spotify_client_id` / `spotify_client_secret` (env, optional) |
| `app/matching.py` (12) | none expected — consumed as-is |
| `app/jobs.py` | batch job gains optional `playlist_spec` (name + m3u recreation) |
| `app/pipeline.py` | reuse/expose the m3u writer for import playlists (read first; extend only if the existing writer is too coupled) |
| `app/pages/import_.py` (12) | URL input tab next to the textarea tab |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_playlist_import.py` (new) | see Testing |

## Step plan

### 1. Parsers (`app/playlist_import.py`)

```python
@dataclass(frozen=True)
class ImportedPlaylist:
    name: str
    source: Literal["spotify", "apple"]
    source_id: str                      # for the stable folder hash
    tracks: list[ImportedTrack]         # artist, title, album | None

def detect_import_url(url: str) -> tuple[str, str] | None   # (source, id)
def fetch_spotify(kind: str, id: str) -> ImportedPlaylist
def fetch_apple(url: str) -> ImportedPlaylist
```

- **Spotify, plain httpx** (no spotipy — two endpoints don't justify a dep):
  1. `POST accounts.spotify.com/api/token` (client credentials, Basic auth from the
     env vars) — cache the token module-level until expiry.
  2. `GET api.spotify.com/v1/playlists/{id}/tracks?fields=…&limit=100` with `next`
     pagination (albums: `/v1/albums/{id}/tracks`). Artist = join of the track's
     artist names with `", "` (the matcher normalizes anyway); title = track name.
  - Local-files/podcast entries in playlists (`track: null` or `is_local`) →
    skipped-lines report, not errors.
- **Apple**: `GET music.apple.com/...` with a browser-ish UA; extract the
  `<script id="serialized-server-data">` JSON; navigate to the track sections
  (verify the current shape against a live page during development — codify the
  path with defensive `.get()` chains and ONE clear `AppleParseError`). Fragility is
  contained: one function, one error type, UI shows `t("import.apple_failed")`.
- Host validation in `detect_import_url` — exact host allowlist, reject everything
  else before any fetch.

### 2. Review flow integration

`app/pages/import_.py` gets two tabs: "Paste list" (12) and "Playlist URL" (this
feature). The URL tab: input → `run.io_bound(fetch_*)` → the fetched
`(artist, title)` list flows into the SAME `match_all` + review table code path —
zero duplication; plus a header showing playlist name/track count and the
"recreate as playlist (m3u)" checkbox (visible only for WebDAV destination).

### 3. m3u recreation (`jobs.py` + `pipeline` reuse)

- `start_batch(…, playlist_spec=PlaylistSpec(name, source, source_id) | None)`.
- After the batch fan-out finishes: collect `rel_path` for every delivered track
  (the delivery already records them via `_record_delivered_safe` — capture the
  values in the batch runner directly rather than re-querying), plus `rel_path`
  from `load_index_paths` for review-step "already on server" tracks.
- Folder name: `_safe_segment(name) + f" [import-{short_hash(source + source_id)}]"`
  — stable per source playlist (future sync could target it), colliding never with
  yt playlist folders (different suffix shape).
- Write the `.m3u8` locally (reuse `_write_m3u`'s conventions: UTF-8, bare filename
  for in-folder — here there are none — and `posixpath.relpath(rel_path, folder)`
  for references; if `_write_m3u` is too entangled with the playlist pipeline,
  factor a small shared `write_m3u_lines(path, lines)` out of it rather than
  copying), upload the folder via the existing upload path.
- Preserve source playlist ORDER (the m3u is ordered by the imported list, not by
  download completion).

### 4. Config & docs

`app/config.py`: two optional str fields (pydantic-settings picks up env
automatically); `.env.example` entries with a comment linking the Spotify developer
dashboard. UI: Spotify tab section hidden when unset (check
`settings.spotify_client_id` truthiness server-side).

## Testing (`tests/test_playlist_import.py`, all offline)

- `detect_import_url`: spotify playlist/album, apple playlist/album, wrong hosts →
  `None`, lookalike hosts (`api.spotify.com.evil.tld`) → `None`.
- Spotify parser against fixture JSON: pagination stitching, `is_local`/null-track
  skipping, artist joining.
- Apple parser against a saved fixture page: happy path + mutilated HTML →
  `AppleParseError`.
- m3u recreation: given delivered + referenced rel_paths and a folder name → exact
  expected file content (ordering, relpaths); collision-free folder naming.
- Batch integration: `playlist_spec` triggers m3u write/upload with fake client
  (extend `tests/test_jobs.py`).

## Definition of done

Acceptance criteria pass; manual verification with one real public Spotify playlist
and one Apple playlist (small), checking the recreated playlist in Navidrome; suite
green; version bumped; PR.
