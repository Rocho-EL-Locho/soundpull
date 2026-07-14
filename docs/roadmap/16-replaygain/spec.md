# 16 — ReplayGain tagging (opt-in)

**Phase:** 4 — Comfort · **Effort:** M · **Depends on:** — (05 for backfill) · **Issue:** —

## Goal

Optionally write **ReplayGain 2.0** loudness tags (track gain/peak + album gain/peak)
into delivered files so Navidrome can volume-normalize playback — no more jumping
between a quiet 90s album and a loud modern master.

## Parity stance (read before anything else)

Writing additional tags **is a deviation** from the frozen tag output. It follows
the established pattern of deliberate, opt-in deviations (like the artist-separator
feature 09 and the documented 0.8.8 feat-in-artist deviation):

- New `UserSettings.write_replaygain`, **default `False`** → default output stays
  byte-identical (guarded by a byte-identity test).
- When enabled, tags are **added** in a separate post-tagging step — the frozen
  `fix_music_tags.py` module and the yt-dlp flag lists are untouched.

## How gain is computed

- **ffmpeg only** (already a hard requirement — no new binary like `rsgain`):
  measure integrated loudness + true peak per track via the `ebur128` filter
  (decode-only second pass over each staged file).
- ReplayGain 2.0 reference: −18 LUFS → `track_gain_db = -18 − measured_LUFS`.
- **Album gain**: one combined measurement over the album's tracks (concat
  measurement or energy-weighted aggregation — implementation decides, documented
  there) per staged album folder; playlist mode gets track gain only (a playlist
  folder is not an album).

## Tag mapping (what Navidrome reads)

| Format | Frames/keys |
|---|---|
| MP3 | `TXXX:REPLAYGAIN_TRACK_GAIN`, `TXXX:REPLAYGAIN_TRACK_PEAK`, `TXXX:REPLAYGAIN_ALBUM_GAIN`, `TXXX:REPLAYGAIN_ALBUM_PEAK` |
| M4A | `----:com.apple.iTunes:REPLAYGAIN_*` freeform atoms |
| Opus/OGG | `REPLAYGAIN_*` vorbis comments (values like `-3.25 dB`) |

## Scope

**In:**

- Settings toggle (metadata card) with a hint (playback normalization, adds ~one
  decode pass per track to download time).
- Pipeline step after tagging / before lyrics+delivery: compute + write tags for
  all three formats; **non-fatal** like cover/lyrics (a measurement failure logs and
  skips that file, never fails the job).
- Applies to all modes and both destinations; artist runs measure per release
  (album gain per album folder).
- A **backfill note** for feature 05: once 05 exists, "missing ReplayGain" becomes
  another deep check + fix using the same computation module (add it to 05's check
  registry then — one sentence of coupling, not a dependency now).

**Out:**

- Re-encoding or actually changing audio loudness (tags only — lossless by
  definition).
- R128_* Opus-specific gain frames (Navidrome handles REPLAYGAIN_* uniformly; keep
  one convention).
- Per-user reference-loudness configuration (fixed −18 LUFS, the RG2 standard).

## Acceptance criteria

1. Toggle off (default): output byte-identical to today — proven by a test that
   hashes a processed file with the feature disabled.
2. Toggle on: an album download yields all four tags on every track (equal
   album-gain values across the album), formats MP3/M4A/Opus each correct;
   Navidrome displays/uses the gain (manual check).
3. A playlist download writes track gain/peak only.
4. Measurement failure on one file → that file delivered untagged-for-RG, job still
   `done`, log line present.
5. Values sane: a loud modern master gets a negative track gain, quiet material
   positive; peak ∈ (0, ~1.2].
6. i18n complete (de + en); parity suite green.
