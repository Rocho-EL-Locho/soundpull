# 06 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names. **Read feature 02's
`sources.py` first** — this feature is mostly filling in its extension points.

## Touch points

| File | Change |
|---|---|
| `app/sources.py` | register `SOUNDCLOUD` spec; add `trust_uploader_as_artist` field |
| `app/pipeline.py` | per-source artist enumerator dispatch; uploader-trust in crediting; SC artwork picker; preview skip |
| `app/pages/index.py` | nothing structural (source detection from 02 does the work); check hint texts |
| `app/i18n.py` | new keys (skip-warning etc., de + en) |
| `tests/test_sources.py`, `tests/test_pipeline.py`, `tests/test_soundcloud.py` (new) | see Testing |

## Step plan

### 1. Source registration (`app/sources.py`)

```python
SOUNDCLOUD = SourceSpec(
    key="soundcloud", label="SoundCloud",
    extractor_args=None,
    supports_cookies=False, supports_pot=False,
    supports_artist=True,
    cover_square_crop=True,          # harmless no-op on square art
    trust_uploader_as_artist=True,   # NEW field, False for YOUTUBE
    matches=_match_soundcloud,       # soundcloud.com, m., on.soundcloud.com
    suggest_mode=_suggest_sc_mode,   # table from spec.md
)
```

- `_suggest_sc_mode`: path-segment based (`/sets/` → album; two segments → single;
  one segment or `/tracks|/albums|/sets` tab → artist; `/likes|/reposts` → return
  a sentinel the UI maps to "unsupported" error).
- Add `trust_uploader_as_artist: bool = False` to `SourceSpec` (YouTube default
  False — **the YouTube crediting rules in `_credits_artist` must not change**).

### 2. Flag derivation (already built in 02)

`_apply_source(_ALBUM_FLAGS, SOUNDCLOUD)` drops the `--extractor-args youtube:…`
pair. Verify no other YouTube-only flags exist in the lists (read them — e.g.
cookie/POT are opts-level, not flags, so nothing else should need stripping).

### 3. Artist enumeration dispatch (`app/pipeline.py`)

- Introduce `enumerate_artist_for(source, url)`; move the current body to
  `_enumerate_artist_youtube` untouched.
- `_enumerate_artist_soundcloud(url)`: normalize to the bare user URL, then flat-probe
  `…/albums` and `…/tracks` with `extract_flat` (same probe opts style as
  `_probe_playlist`): albums → one release per set; standalone tracks not contained
  in any enumerated album → grouped as singles (the artist orchestrator
  `run_artist_download` already handles single-track releases from the YT path —
  match the release-dict shape it expects; read its docstring/usage first).
- The artist display name comes from the profile probe (`uploader`); it feeds
  `own_artist` exactly like the YT path (never the `_UNKNOWN_ARTIST` fallback rule —
  that logic is shared and stays).

### 4. Crediting with uploader trust

In `_credits_artist` (or its caller building the match filter), extend the credit
sources **only when** `source.trust_uploader_as_artist`:

- credit tags list stays first (some SC uploads do carry `artist`);
- then `uploader` counts as a credited name.

The word-boundary matching, `_prefix_artists` collab splitting, title repair
(`_repair_broken_title`) and prefix stripping (`_strip_own_artist_prefix`) all apply
unchanged — they were built for exactly this `Artist - Title` shape. **Do not** relax
anything for the YouTube path; thread the flag explicitly, default False.

### 5. Cover artwork

`pick_square_cover` prefers YT `sqp=` URLs; add a SoundCloud branch: from the probed
`thumbnails`, prefer the `original`/largest square variant, upgrading
`-large.jpg` → `-t500x500.jpg` when only that naming is present. `_square_crop_jpeg`
stays in the chain (no-op on square input).

### 6. Preview/Go+ tracks

At probe/enumerate time a preview-only track shows a truncated duration/preview
format note. Detection: in the match filter's probe path, if the entry's available
formats are all `preview`-flagged (or `duration` < a threshold vs `full_duration`
when present) → **reject via match_filter** with a distinct reason, and count it into
`expected`/`failed` the same way throttled tracks flow into
`Result.failed_count` → `jobs.partial_delivery`. Add an i18n-keyed warning suffix so
the history entry says why. (Investigate the exact yt-dlp fields against a real Go+
track early — this is the one genuinely uncertain part; if reliable detection proves
impossible, fall back to a post-download duration sanity check before tagging.)

### 7. Subscriptions

`subscriptions.py` validates via `is_supported_url` — already source-aware after 02.
Verify `_run_sync`'s probe path has no `youtube:` assumptions (it uses the generic
probe helpers; the extractor-args now come per-source from 02).

## Testing

- `tests/test_sources.py`: SC URL detection + mode-suggestion table (incl. rejected
  `/likes`), short-link host.
- `tests/test_pipeline.py`: `_apply_source` for SC contains no `youtube:` args;
  **YouTube snapshots unchanged**.
- `tests/test_soundcloud.py` (all offline, fake probe payloads):
  - enumeration: albums + loose tracks → release list shape matches
    `run_artist_download` expectations; tracks already in an album not duplicated
    as singles.
  - crediting: uploader accepted as credit only with the flag; YT path regression
    (flag False → uploader still ignored).
  - artwork URL upgrade logic.
  - preview detection predicate (table of fake format lists).
- **Manual verification (mandatory, document in the PR):** one real public SC track,
  one set, one small artist — check tags in Navidrome, square cover, dedup on second
  run; plus one real YouTube album to confirm parity.

## Definition of done

Acceptance criteria pass; manual matrix above done; suite green; version bumped;
PR references issue #30 ("Closes #30").
