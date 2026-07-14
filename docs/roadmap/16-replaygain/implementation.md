# 16 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names.

## Touch points

| File | Change |
|---|---|
| `app/replaygain.py` | **new** — measurement (ffmpeg ebur128) + tag writing (3 formats) |
| `app/pipeline.py` | post-tagging hook: `if fetch_replaygain: apply_replaygain(dir)` (mirror the lyrics-step wiring) |
| `app/jobs.py` | thread the setting into pipeline calls (all three run paths — copy how `fetch_lyrics` flows) |
| `app/models.py` | `UserSettings.write_replaygain: bool = Field(default=False)` |
| `app/pages/settings.py` | toggle + hint in the metadata card |
| `app/i18n.py` | keys (de + en) |
| `tests/test_replaygain.py` (new), `tests/test_pipeline.py` | see Testing |

## Step plan

### 1. Measurement (`app/replaygain.py`)

```python
@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float
    true_peak: float          # linear (10 ** (dBTP / 20))

def measure(path: Path) -> Loudness:
    # ffmpeg -hide_banner -nostats -i <path>
    #   -af ebur128=peak=true -f null -
    # parse the summary block from stderr (I: … LUFS, True peak: … dBTP)

def track_gain_db(l: Loudness) -> float:   # -18.0 - integrated_lufs
def album_loudness(tracks: list[Loudness]) -> Loudness:
    # energy-average: mean of 10**(lufs/10) back to dB; peak = max
```

- Parse defensively (locale-independent — ffmpeg stderr is stable English); raise
  one `ReplayGainError` on any parse/measure failure.
- The summary-parsing approach avoids `framelog=verbose` floods; verify the exact
  summary format against the ffmpeg version in the Docker image (pin-check in the
  PR, note the tested version in a comment).

### 2. Tag writing

```python
def write_rg_tags(path: Path, track: Loudness, album: Loudness | None) -> None
```

- mutagen per format, following the **same file-type dispatch** the codebase already
  uses (see how `app/lyrics.py` / the fix_music_tags adapters detect format — reuse
  the detection, do not invent a fourth way).
- Value formatting per RG2 convention: gains as `"%.2f dB"`, peaks as `"%.6f"`.
- MP3: `TXXX` frames with the exact `desc` strings from the spec table (ID3v2.3,
  matching what the file already is — do not touch version/other frames).
- M4A: `----:com.apple.iTunes:REPLAYGAIN_…` freeform (bytes, utf-8).
- Opus/OGG: uppercase vorbis comment keys.

### 3. Pipeline hook (`app/pipeline.py`)

`apply_replaygain(staged_dir: Path, *, is_album: bool, log) -> None`:

- collect audio files (same glob set the m3u/index building uses), `measure` each,
  compute album loudness only when `is_album` (album/single modes and artist-run
  releases: yes; playlist folder: no), `write_rg_tags` each — **per-file
  try/except** logging failures (best-effort contract, mirror the lyrics step's
  non-fatal wiring and its placement: after tagging, before delivery, so the
  m3u/index/upload all see final files).
- Call sites: exactly where `fetch_lyrics` hooks in — same conditional structure,
  new flag `write_replaygain` threaded identically (jobs → `run_download` /
  `run_artist_download` → per-release; `_SyncConfig` gains the field for interval
  syncs).

### 4. Settings/i18n

Toggle in the metadata card next to the lyrics toggle (visually a sibling —
both are "extras written next to/into files"); hint text mentions added download
time. Keys in de + en.

## Testing (`tests/test_replaygain.py`)

- Parser: fixture stderr blocks (normal, silent track −70 LUFS, missing summary →
  `ReplayGainError`).
- Gain math: table (−8 LUFS → −10.0 dB gain; −23 → +5.0); album energy-average
  known-values; peak conversion.
- Tag writing round-trip on synthetic ffmpeg files for all three formats (the
  fix_music_tags test suite has the generation pattern — reuse its helpers/fixtures)
  asserting exact frame names/values and **no other frame changed** (full tag dict
  compare before/after, minus the four new keys).
- Byte-identity guard: `write_replaygain=False` → pipeline output hash unchanged
  (extend the existing parity test).
- Pipeline wiring: monkeypatched `apply_replaygain` called with correct `is_album`
  per mode (extend `tests/test_pipeline.py` in the style of the lyrics-step tests).

## Definition of done

Acceptance criteria pass; manual verification: one real album with the toggle on,
tags inspected (`ffprobe`/mutagen) and normalization audibly working in Navidrome
(enable "ReplayGain" in its player settings); suite green; version bumped; PR.
