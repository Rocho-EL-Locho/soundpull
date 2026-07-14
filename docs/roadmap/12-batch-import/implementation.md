# 12 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names. **Requires feature
07 merged** (`app/search.py`).

## Touch points

| File | Change |
|---|---|
| `app/matching.py` | **new** — parse, match, confidence scoring (pure + search calls) |
| `app/jobs.py` | batch job type (`start_batch` / `_run_batch`) |
| `app/pages/import_.py` | **new** — `import_content()` (`/import` route; module named with trailing underscore — `import` is a keyword) |
| `app/main.py`, `app/theme.py` | route + nav entry |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_matching.py` (new), `tests/test_jobs.py` | see Testing |

## Step plan

### 1. `app/matching.py`

```python
@dataclass(frozen=True)
class ParsedLine:
    raw: str; artist: str | None; title: str | None; error: str | None

@dataclass
class Match:
    line: ParsedLine
    candidates: list[SearchResult]      # from app.search, songs only, top 3
    best: SearchResult | None
    confidence: float                   # 0..1
    on_server: bool

def parse_lines(text: str) -> list[ParsedLine]: ...
def score(parsed: ParsedLine, result: SearchResult) -> float: ...
def match_all(user_id: int, lines: list[ParsedLine],
              progress: Callable[[int, int], None] | None = None) -> list[Match]: ...
```

- `parse_lines`: split on `\n`; per line try in order: CSV row (if the first line
  looks like a header `artist,title`), tab split, ` - ` / ` – ` split (first
  separator only — a title containing ` - ` stays intact because the FIRST segment
  is the artist by contract). Trim, drop empties, mark failures with an i18n-able
  error key.
- `score`: `difflib.SequenceMatcher` ratio over casefolded, feat-stripped strings —
  reuse `library_index`'s normalization helpers (`_norm`, `_clean_title`) so
  "already in library" and confidence agree on what equal means. Combined score =
  min(artist ratio, title ratio) — both must match, an exact title with the wrong
  artist must not pass.
- `match_all`: sequential search calls with `progress(i, n)` callback (ytmusicapi is
  fast enough sequentially; parallelize only if measured too slow — and then max 3
  workers). `on_server` via one upfront `load_index_paths(user_id)` dict, key
  membership per candidate.

### 2. Batch job (`app/jobs.py`)

- Follow `_run_artist`'s structure (one job id, fan-out, aggregate results):
  `start_batch(user_id, items: list[url], genre, audio_format, destination, …)` →
  `_run_batch` loops (or uses a small inner pool like the artist run — start
  sequential, it's simpler and the outer pool already bounds concurrency) over
  single-mode `run_download` calls, updating `JobState.current_track/total_tracks`
  per item, collecting failures into `failed_tracks`.
- History row: `mode="batch"` — check every `mode` consumer:
  `history.py` filter options, `retry_options` (retry a batch = re-run remaining/
  failed URLs — store the URL list in the history `log`/a JSON column the way
  playlist manifests are stored, or simplest: make batch retry re-run only failed
  URLs persisted in `DownloadHistory.log` events; decide while reading
  `retry_options`, keep it consistent with how artist runs retry today).
- Dedup: pass the standard `on_server` machinery so an already-present track is a
  cheap skip even if the user left it checked.

### 3. Page (`app/pages/import_.py`)

- Stepper UI (`ui.stepper` or simple state machine): paste → match (progress bar via
  `ui.timer` on module-level state, like feature 04's analysis) → review table
  (`ui.table` with checkbox column; per-row `ui.select` when >1 candidate;
  confidence as colored badge; on-server badge) → confirm dialog (count summary) →
  `start_batch` → link to the job card on the index page.
- Cap check with clear error above the textarea (`t("import.too_many_lines")`).

### 4. Routing/nav/i18n

As usual: route in `main.py`, nav entry in `frame()`, keys in both languages.

## Testing

- `parse_lines` table: all accepted shapes, en dash, CSV with/without header, junk
  lines → error entries, size cap.
- `score`: exact match ≈ 1.0; wrong artist with exact title scores low; feat
  variants score high (normalization shared with `track_key`).
- `match_all` with monkeypatched `search_music`: candidate selection, on_server
  flagging, progress callbacks.
- Batch job: `_run_batch` with monkeypatched `run_download` — counting, partial
  failure → `failed_tracks`, history row shape (extend `tests/test_jobs.py`).
- No live ytmusicapi calls in tests.

## Definition of done

Acceptance criteria pass; manual smoke: paste a 5-line real list, review, download,
verify tags + history row; suite green; version bumped; PR.
