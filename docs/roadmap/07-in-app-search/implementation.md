# 07 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `pyproject.toml` | add pinned `ytmusicapi` dependency |
| `app/search.py` | **new** — search + result normalization + URL building |
| `app/pages/index.py` | search row + results UI above the URL input |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_search.py` (new) | see Testing |

## Step plan

### 1. `app/search.py`

```python
@dataclass(frozen=True)
class SearchResult:
    kind: Literal["song", "album", "artist", "playlist"]
    title: str
    artist: str          # subtitle for artists/playlists
    url: str | None      # None for albums until resolved (see below)
    browse_id: str | None
    thumbnail: str | None

def search_music(query: str, limit: int = 5) -> list[SearchResult]: ...
def resolve_album_url(browse_id: str) -> str: ...   # get_album → audioPlaylistId → OLAK5uy_ URL
```

- One module-level `YTMusic()` instance, created lazily inside a function (import of
  `ytmusicapi` also inside the function — keeps app startup independent of the dep
  and makes the failure mode local).
- Use `ytmusic.search(query, filter=...)` per category (`songs`, `albums`,
  `artists`, `playlists`) with small limits, or one unfiltered call + client-side
  grouping — pick whichever yields better albums (verify against the live API once
  during development; filtered calls are the documented-stable path).
- URL building: song `videoId` → `https://music.youtube.com/watch?v=<id>`;
  artist `browseId` (UC…) → `https://music.youtube.com/channel/<id>`;
  playlist `browseId`/`playlistId` (VL-prefix strip) →
  `https://music.youtube.com/playlist?list=<id>`;
  album → **defer** the extra `get_album` call to click time via
  `resolve_album_url` (albums' `browseId` is `MPREb_…`, not directly downloadable).
- Every public function catches exceptions and raises one typed `SearchError` with a
  short message; timeout kept tight (the library uses `requests` — pass
  `requests_session` with a timeout adapter, or wrap calls with a bounded
  ThreadPool future timeout — simplest robust option given `run.io_bound`).

### 2. UI (`app/pages/index.py`)

- New collapsed-by-default section (or always-visible slim row) **above** the URL
  input: `ui.input` (search) + `ui.button`; `props('clearable')`; Enter triggers
  search.
- Handler: `results = await run.io_bound(search_music, query)` → render grouped
  cards into a `@ui.refreshable` container: thumbnail (`ui.image`, small,
  rounded), title, artist, kind badge (`ui.badge`).
- Click handler per result:
  - `kind == "album"` → `url = await run.io_bound(resolve_album_url, browse_id)`
    (spinner on the card while resolving)
  - set the URL input's value, set the mode toggle (this must trigger the toggle's
    change handler so artist-mode side effects like the dedup default keep working —
    same concern as feature 02's auto-suggestion; if 02 is merged, its
    `manual_mode_override` flag should treat this click as a *manual* choice).
  - clear/collapse the results.
- `SearchError` → `ui.notify(t("search.failed"), type="warning")`.
- Keep the whole section behind `if settings.search_enabled`? No — no config flag
  needed; the feature is stateless and fails soft. (Decision: no new setting.)

### 3. i18n

Keys: `search.placeholder`, `search.button`, `search.songs`, `search.albums`,
`search.artists`, `search.playlists`, `search.failed`, `search.no_results` — de + en.

## Testing (`tests/test_search.py`)

All offline — monkeypatch the `YTMusic` object:

- normalization: fake `search` payloads (copy real response shapes into fixtures) →
  `SearchResult` lists; missing thumbnails/fields tolerated.
- URL building table: videoId/channel/playlist/VL-prefix stripping.
- `resolve_album_url`: fake `get_album` payload → `OLAK5uy_` URL; missing
  `audioPlaylistId` → `SearchError`.
- error wrapping: underlying exception → `SearchError`, message contains no stack.

**Manual verification:** live search for one artist, download one song + one album
end-to-end from a click; confirm the pinned ytmusicapi version works against the live
API at merge time.

## Definition of done

Acceptance criteria pass; manual verification done; suite green; version bumped;
PR references issue #41 ("Closes #41").
