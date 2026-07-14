# 05 — Library health check

**Phase:** 2 — Manage · **Effort:** L · **Depends on:** 01 · **Issue:** —

## Goal

An audit of the existing library that finds metadata/file problems and fixes the
fixable ones with machinery Soundpull already has — keeping older downloads (made
before newer features existed) up to the app's current standards.

## Checks

Two tiers — **cheap** (path/listing-based, no file downloads) and **deep** (requires
downloading files to staging to read tags):

| # | Check | Tier | Fix |
|---|---|---|---|
| H1 | Missing `.lrc` lyrics sidecar | cheap | existing `backfill_lyrics` (feature 03 adds the prefix param) |
| H2 | Stray files: leftover `.jpg`/`.webp` thumbnails next to tracks, `.part`/`.ytdl` fragments | cheap | trash them (01) |
| H3 | Empty folders | cheap | delete folder |
| H4 | Non-audio junk in album folders (anything not audio/`.lrc`/`cover.*`/`.m3u8`) | cheap | report only (user decides via trash action) |
| H5 | Album split by year: tracks of one album folder carry different `date` tags (the Navidrome "one album per year" symptom) | deep | unify to earliest year — same rule as `pipeline._unify_album_year` |
| H6 | Missing embedded cover art | deep | re-embed from `cover.jpg`/the largest embedded cover found in the same album folder; if none exists → report only |
| H7 | Missing genre tag | deep | write the user's `default_genre` (opt-in per finding) |
| H8 | Missing/empty album or album-artist tag | deep | report only (fixing would guess) |
| H9 | Corrupt/truncated audio file (fails an ffmpeg decode pass) | deep | report only (points the user at re-downloading; a broken file is never auto-deleted) |

Deliberately honest scoping: fixes only where a **correct** value is derivable
(existing sidecar machinery, in-folder cover, earliest year). No internet lookups of
"better" metadata — that would fight the parity philosophy and invite wrong tags.

## Current state

- `backfill_lyrics` (`app/library_index.py`) already implements the
  download-check-upload-sidecar loop for H1 — the pattern to generalize.
- `_unify_album_year` exists in `app/pipeline.py` but operates on **staged local
  files before upload**; H5 needs the same rule applied to a remote album via
  download → retag → re-upload.
- `fix_music_tags.py` is **frozen** — health fixes use mutagen directly (or the
  format adapters' read helpers), never modify that module.

## Scope

**In:**

- `app/health.py` with a small check registry (id, tier, detect, fix?) so future
  checks are one entry.
- Cheap checks run over the same walk the duplicate finder uses (feature 04's
  `iter_library_files` refactor — if 04 isn't merged yet, do that refactor here;
  whichever lands first).
- Deep checks run **per album on demand** ("check this album") and as a bounded batch
  ("deep-check N albums", default 25 per run) — never an unbounded full-library
  download.
- UI: a "Health" page (or tab beside the library page): one card per check with
  finding count, expandable finding list, and a fix button where a fix exists.
  Deep-check progress like the analysis in feature 04.
- All destructive fixes go through the trash (01); tag-rewriting fixes re-upload the
  changed file only.

**Out:**

- Online metadata enrichment (MusicBrainz etc.).
- Fixing files that aren't in the app's three supported formats (MP3/M4A/Opus —
  report as H4 junk instead).
- Automatic scheduled health runs (manual trigger only in this iteration).

## Acceptance criteria

1. Cheap checks find seeded problems (missing `.lrc`, stray `.jpg`, empty folder) in
   one walk without downloading any audio file.
2. H5 fix on a seeded two-year album folder results in all tracks carrying the
   earliest year, only the changed files re-uploaded, tags otherwise byte-identical
   (verify with mutagen before/after comparison in the test).
3. H6 re-embeds a cover from `cover.jpg` in the folder; a track that already has art
   is not touched.
4. Deep batch respects its bound and is resumable (next batch continues where the
   last stopped).
5. Every fix is logged in the finding list with a result; failures are per-file,
   never abort the run (best-effort pattern).
6. `fix_music_tags.py` and the pipeline flag lists are untouched; full suite green.
7. i18n complete (de + en).
