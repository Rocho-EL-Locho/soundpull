# 05 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/health.py` | **new** — check registry, cheap/deep runners, fixes |
| `app/library_index.py` | reuse `iter_library_files` (from 04; create here if 04 not merged) |
| `app/models.py` | **new table** `HealthReport` (JSON, per user — same shape as `DuplicateReport`) |
| `app/pages/health.py` | **new** — `health_content()` |
| `app/main.py`, `app/theme.py` | route + nav entry |
| `app/i18n.py` | new keys (de + en) |
| `tests/test_health.py` (new) | see Testing |

## Design

### Check registry

```python
@dataclass(frozen=True)
class Check:
    id: str                     # "lyrics_missing"
    tier: Literal["cheap", "deep"]
    detect: Callable[..., list[Finding]]
    fix: Callable[[Finding], FixResult] | None    # None => report-only

CHECKS: tuple[Check, ...] = (...)
```

`Finding`: `check_id`, `rel_path` (file or folder), `detail` (human string), `fixable`.
Report persisted as JSON in `HealthReport` (replace-on-rerun, like 04).

### Cheap pass (one walk)

Single traversal via `iter_library_files` collecting per-folder file lists, then:

- **H1** missing lyrics: audio file without sibling `<stem>.lrc` — only when the user
  has `fetch_synced_lyrics` enabled (otherwise the whole check is skipped/greyed).
- **H2** strays: `*.jpg/*.jpeg/*.webp/*.png` that are not `cover.*`, plus
  `*.part/*.ytdl` — the pipeline normally deletes thumbnails before delivery, so any
  survivor is from an old version or an interrupted run.
- **H3** empty folders (from the dir listings of the same walk).
- **H4** other extensions (not audio / `.lrc` / `cover.*` / `.m3u8`).

### Deep pass (per album, bounded)

```python
def deep_check_album(user_id, album_prefix: str) -> list[Finding]:
    # download all audio files of the folder to a scratch dir under LOCAL_MUSIC_ROOT
    # (staging only!), read tags with mutagen, evaluate H5–H8, clean up scratch
```

- Batch runner: iterate album folders sorted, skip albums already covered by the
  current report (`checked_albums` list inside the report JSON → resumability),
  stop after `limit` (default 25).
- Tag reads: use mutagen directly (`MP3`/`MP4`/`OggOpus`), matching the formats the
  app writes. Read helper mapping frame names per format — model it on how
  `write_lrc_for` in `app/lyrics.py` reads final tags (that is the precedent for
  "read tags without touching fix_music_tags").
- **H9 integrity**: on the already-downloaded local copy run
  `ffmpeg -v error -i <file> -f null -` (decode-only, no output file); any stderr
  output → finding with the first error line as detail. This piggybacks on the same
  download the tag checks need — no extra transfer. ffmpeg is a hard runtime
  requirement already.

### Fixes

- **H1** → call `backfill_lyrics(user_id, prefix=album_prefix)` (feature 03's param;
  add it here if 03 hasn't landed).
- **H2/H3** → `library_ops.trash_track` / folder delete (01).
- **H5** → port the *rule* of `pipeline._unify_album_year` (earliest `date` wins) into
  `health.py` operating on the downloaded copies, then re-upload **only changed
  files** via the 01 upload path (`client.upload_file` with the retry wrapper).
  Do not call the pipeline function itself if its signature is staging-coupled; the
  rule is three lines — duplicating the rule with a comment pointing at the origin is
  acceptable *only if* reuse is genuinely awkward; prefer extracting a shared pure
  helper `unify_year(dates: list[str]) -> str` into a neutral module both import.
- **H6** → embed `cover.jpg` (or largest embedded art in the folder) using the same
  mutagen APIC/covr/picture writes the format adapters in `fix_music_tags.py` use —
  **read** that module for the exact frame conventions (APIC encoding, covr format,
  Ogg picture block) and mirror them; do not modify it.
- **H7** → write `default_genre` per format; opt-in checkbox per finding batch.

All fixes best-effort per file (log + continue), results written back into the report.

## UI (`app/pages/health.py`)

- Header: last run, [Run cheap checks] button, [Deep-check next 25 albums] button,
  album picker for a targeted deep check.
- One card per check: count badge, expandable finding table (path + detail), fix
  button (with confirm dialog stating exactly what will change), per-check "select
  all" where fixes are opt-in (H7).
- Progress via the same in-memory-state + `ui.timer` pattern as feature 04's analysis.

## Testing (`tests/test_health.py`)

- Cheap detectors on synthetic walk data (tuples of paths) — every check, positive +
  negative cases.
- `unify_year` pure helper: earliest-wins, single-year no-op, missing dates.
- H5 end-to-end on **local** synthetic files (ffmpeg-generated silent MP3s, as in
  the fix_music_tags round-trip tests): seed two years, run fix, assert dates unified
  and other frames byte-identical.
- H6: file without APIC + folder cover → embedded; file with APIC → untouched.
- Resumability: report with `checked_albums` skips them on the next batch.
- No network in tests (fake client for the walk/download/upload seams).

## Definition of done

Acceptance criteria pass; manual smoke against a copy of the real library (run cheap
checks, fix a seeded stray, deep-check one album); suite green — **including the
untouched `test_fix_music_tags.py` / `test_pipeline.py`**; version bumped; PR.
