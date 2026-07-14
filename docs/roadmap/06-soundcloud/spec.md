# 06 — SoundCloud support

**Phase:** 3 — Grow · **Effort:** M · **Depends on:** 02 · **Issue:** #30

## Goal

Paste a SoundCloud URL (track / set / artist page) and get the same treatment as a
YouTube link: Navidrome-tagged files, square cover, lyrics, dedup, ZIP or WebDAV —
implemented as a **registry entry** on the feature-02 source architecture.

## Why it fits

yt-dlp supports SoundCloud natively (no PO tokens, no player-client juggling), and the
whole download/tag/deliver core of `run_download` is extractor-neutral. What is needed
is the source registration plus handling of SoundCloud's metadata quirks.

## URL → mode mapping

| URL shape | Mode |
|---|---|
| `soundcloud.com/<user>/<track>` | `single` |
| `soundcloud.com/<user>/sets/<set>` | `album` (user can switch to `playlist` for mix-style sets) |
| `soundcloud.com/<user>` (also `/tracks`, `/albums`, `/sets` tabs) | `artist` |
| `on.soundcloud.com/<short>` | resolve like the target (yt-dlp follows) |
| `soundcloud.com/<user>/likes`, `/reposts` | **rejected** in this iteration |

## SoundCloud metadata quirks (drive the implementation)

- **Artist**: SoundCloud has no structured artist credit — `uploader` is the artist in
  the common case, and titles frequently carry `Artist - Title`. This is the inverse
  of YouTube's rule (where `uploader`/`channel` must NOT be trusted, see
  `_credits_artist`). The source spec therefore needs a
  **`trust_uploader_as_artist`** flag that the artist-mode match filter and the
  probe fallback respect per source.
- **Title repair**: the existing `<Artist> - <Song>` repair
  (`_repair_broken_title` / `_strip_own_artist_prefix`) matches SoundCloud reality
  well — it should apply in artist mode exactly as for YouTube.
- **Cover**: SoundCloud artwork is already square (`t500x500`); the source spec sets
  `cover_square_crop=False` conceptually, but `_square_crop_jpeg` is a no-op on
  square images anyway — prefer upgrading the artwork URL to the largest variant
  (`t500x500` → `original` with fallback).
- **Genre**: SoundCloud exposes a real `genre` field — ignored for tagging (the
  user-selected genre from the form stays authoritative, as everywhere in the app).
- **Availability**: Go+/preview-only tracks (~30 s snippets) must be **skipped with a
  warning**, not delivered as snippets: detect via duration mismatch/`preview` format
  notes at probe time and count them into the existing partial-delivery surfacing
  (`failed_tracks` / `jobs.partial_delivery`).
- **Formats**: streams are Opus/MP3 (some HLS — ffmpeg already required). The
  `AUDIO_FORMATS` transforms are codec-level and apply unchanged; `original` yields
  whatever SoundCloud serves.

## Scope

**In:**

- `SourceSpec` registration (hosts, mode suggestion per the table, no extractor-args,
  no cookies/POT in this iteration, artist mode supported).
- Per-source artist enumeration: SoundCloud `/albums` + `/tracks` tabs via
  yt-dlp flat extraction (the YT `enumerate_artist` stays untouched; feature 02's
  dispatch point selects per source).
- Artist-mode crediting adjustments behind the `trust_uploader_as_artist` flag.
- Preview-track skip + warning wiring.
- Subscriptions: SoundCloud set URLs are valid `PlaylistSubscription`s (the sync
  pipeline is source-agnostic once detection works).

**Out:**

- SoundCloud login/OAuth (Go+ full streams), likes/repost scraping.
- SoundCloud search (`scsearch:` — future extension of feature 07).

## ⚠️ Parity constraint

YouTube outputs must remain byte-identical: SoundCloud gets **derived** flag lists via
feature 02's `_apply_source` (drops the `youtube:` extractor-args), while
`_ALBUM_FLAGS`/`_SINGLE_FLAGS` and the whole tag chain stay frozen. The pipeline
options snapshot tests for YouTube must not change.

## Acceptance criteria

1. A public SoundCloud **track** downloads as `single`, tagged by the same
   `fix_music_tags` rules, square cover embedded, delivered to ZIP and WebDAV.
2. A **set** downloads as `album` (one folder, forced artist/album, track numbers).
3. An **artist page** run enumerates albums + standalone tracks, applies
   `own_artist` crediting with uploader trust, dedups against the library index like
   a YouTube artist run.
4. A preview-only track is skipped and surfaces in the partial-delivery warning, not
   as a 30-second file in the library.
5. Lyrics sidecars (LRCLIB is source-agnostic) and playlist m3u generation work
   unchanged for SoundCloud content.
6. All YouTube snapshot/parity tests pass **unchanged**; a real YouTube album
   re-verified once.
7. i18n complete for new strings; suite green.
