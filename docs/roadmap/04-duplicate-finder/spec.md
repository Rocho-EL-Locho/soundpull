# 04 — Duplicate finder & cleanup

**Phase:** 2 — Manage · **Effort:** L · **Depends on:** 01 · **Issue:** —

## Goal

Find duplicates **already sitting in the library**, present them for review with a
sensible keeper suggestion, and clean them up safely (trash, not hard delete) — with
playlist references repaired so nothing breaks.

## Current state — why this doesn't exist yet

All existing dedup is **download-time or staging-time**, never library-wide:

- `library_index` + the yt-dlp `match_filter` *skip* tracks that are already indexed —
  they prevent *new* duplicates, but can't see existing ones.
- `pipeline._dedup_staged_tracks` dedups only within one artist run's staging dir.
- Crucially, the index **cannot represent duplicates at all**: `ServerTrack` has a
  unique constraint on `(user_id, artist_norm, title_norm)`, so when two files map to
  the same key, `scan_webdav`'s upsert keeps one row and the collision is silently
  lost. Detection therefore has to happen **during the walk**, not from the table.

## Duplicate classes to detect

1. **Exact key collisions** — same `track_key(artist, title)` at ≥ 2 paths. Typical:
   the same song as a standalone single folder *and* inside an album; a copy inside a
   playlist folder *and* in the artist tree.
2. **Noise-variant titles** — same core title after stripping release noise:
   `Song (prod. by X)`, `Song (Official Video)`, `Song [Remaster]` vs `Song`.
   Reuse the same noise philosophy as `pipeline._strip_title_noise` (artist-mode
   title repair) — but as a **secondary, clearly-labelled "probable" tier**, never
   auto-selected for deletion.
3. Same title with `(feat. …)` variants and multi-artist orderings — mostly already
   collapsed by `track_key`'s normalization (`_clean_title` strips feat via
   `fix_music_tags.FEAT_PATTERNS`; `_primary_artist` takes the first artist);
   verify with tests rather than building anything new.

Explicitly **not** duplicates: the same title by different artists, live/acoustic
versions when the qualifier is part of the core title after noise-stripping — when in
doubt, the group lands in the "probable" tier for human review.

## Keeper heuristic

Same rationale as `_dedup_staged_tracks`: **the copy in the biggest album folder wins**
(a real release beats a 1-track single; artist tree beats playlist folder). Ties →
shorter path, then lexicographic. The suggestion is *pre-selected*, never auto-applied.

## Scope

**In:**

- Analysis pass (background, with progress): walk the WebDAV library (same traversal
  as `scan_webdav`), collect `key → [paths]`, build exact + probable groups, persist
  the report.
- Review UI (own page `/duplicates`, linked from nav and — once 03 exists — from the
  library page): groups as cards showing each copy (path, folder size in tracks),
  keeper pre-selected, per-group confirm + "accept all exact suggestions" bulk action
  (bulk only for the exact tier).
- Cleanup: non-keepers → **trash** (feature 01), index rows fixed up, and **playlist
  reference repair**: any `.m3u8` line pointing at a deleted copy is rewritten to the
  keeper's path (cross-folder relative, same `posixpath.relpath` frame the pipeline
  uses); affected `PlaylistSubscription.playlist_files` manifests updated the same way.
- Report persistence so the user can leave the page and come back (re-running the
  analysis replaces the report).

**Out:**

- Audio-content fingerprinting (acoustid/chromaprint) — out of scope for this
  iteration; the path/tag-key approach matches how the rest of Soundpull thinks.
- Cross-user dedup (index is per-user by design).
- Auto-deletion without a user confirmation step.

## UX sketch

```
Duplicates                                  [Analyze library]
Last analysis: 2026-07-14 — 9 exact groups, 3 probable

EXACT · Burial – Archangel                       [Keep selected, trash 1]
  (•) Burial/Untrue/05 - Archangel.mp3          album · 13 tracks   ← suggested
  ( ) Burial/Archangel/01 - Archangel.mp3       single · 1 track
      referenced by playlist "Late Night [PL…]" → will be re-pointed

PROBABLE · Song (prod. by X) ↔ Song             [review]
```

## Acceptance criteria

1. Analysis finds a seeded exact duplicate (same key, two folders) and groups it with
   the biggest-album keeper pre-selected.
2. Confirming a group trashes the non-keepers, the index still has exactly one row
   with the keeper's `rel_path`, and a re-scan does not resurrect the duplicates.
3. An `.m3u8` that referenced a removed copy afterwards points at the keeper and the
   playlist still resolves in Navidrome (relative path correct from the playlist
   folder).
4. Probable-tier groups are never included in the bulk action.
5. Analysis is non-blocking (progress visible, UI usable) and re-runnable.
6. Nothing is hard-deleted; every removed file is restorable from the trash.
7. i18n complete (de + en); full test suite green; no pipeline/tagging changes.
