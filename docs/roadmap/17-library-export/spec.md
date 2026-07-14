# 17 — Library export & backup

**Phase:** 2 — Manage · **Effort:** S · **Depends on:** — · **Issue:** —

## Goal

An insurance policy and an interoperability hatch: export **what the collection
contains** (library manifest), **what the app did** (download history), and **how it
is configured** (settings) as downloadable files — so a dead disk, a migration, or a
third-party tool never means starting from zero knowledge.

## Current state

All the data exists (per user: `ServerTrack` index, `DownloadHistory`,
`UserSettings`, subscriptions) but the only way out is opening the SQLite file by
hand on the server.

## Scope

**In — three export buttons on the settings page (new "Export & backup" card):**

1. **Library manifest** — CSV and JSON variants: one row per indexed track
   (`artist`, `title`, `rel_path`, plus display artist/album derived from the path).
   This is the "what do I own" list — re-importable via feature 12's batch import
   (CSV columns intentionally compatible with its parser: `artist,title` header).
2. **Download history** — CSV: url, mode, genre, format, destination, artist, album,
   phase, failed_tracks, created/finished timestamps. (Answers "where did this track
   come from" forever, even outside the app.)
3. **Settings export/import** — JSON of the user's `UserSettings` **minus every
   secret** (`*_enc` fields, passwords, tokens — excluded by field-name convention,
   with a test enforcing that nothing matching `*_enc`/`password`/`token`/`cookie`
   ever serializes). Import merges onto the current settings with a confirm dialog;
   secrets must be re-entered manually afterwards (stated in the dialog).

All exports are **per-user** (the requesting user's data only) and delivered via the
browser (`ui.download` — same mechanism as ZIP delivery), never written into the
music library.

**Out:**

- Full SQLite/DB file download (contains OTHER users' data and encrypted secrets —
  a server-admin concern, solved by documenting a `docker compose` volume-backup
  one-liner in the README instead).
- Automatic/scheduled backups (manual click; automation via the future REST API,
  feature 20).
- Import of library manifests as *index* state (the index's source of truth is the
  scan — importing rows would lie about what's on the server).

## Acceptance criteria

1. Library CSV/JSON exports match the index exactly (row count = `ServerTrack`
   count for the user) and the CSV round-trips through feature 12's parser (once 12
   exists — until then, the header contract `artist,title` is unit-tested).
2. History CSV opens correctly in a spreadsheet (proper quoting/UTF-8 BOM decision
   documented; umlauts intact).
3. Settings JSON contains zero secret material — enforced by a test that fails when
   a new secret-shaped field would leak (future-proofing).
4. Settings import applies values, skips unknown keys with a notice, never touches
   secrets, and requires an explicit confirm.
5. Exports of a fresh user (empty everything) produce valid empty files, no errors.
6. i18n complete (de + en); suite green; zero pipeline/model changes beyond none.
