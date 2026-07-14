# 11 — Bandcamp support

**Phase:** 3 — Grow · **Effort:** S–M · **Depends on:** 02 (learn from 06) · **Issue:** —

## Goal

Accept Bandcamp URLs (track / album / artist page) as a third source — another
registry entry on the feature-02 architecture, structurally a smaller sibling of the
SoundCloud feature (06).

## Honest quality note (belongs in the UI hint too)

Bandcamp **free streams are ~128 kbps MP3** — lossless is purchase-only and not
reachable via yt-dlp. This feature is for completeness/discovery (demos, free
releases, artists that exist nowhere else), not a quality upgrade. The
`original` audio format is the honest default recommendation for Bandcamp (remux
instead of fake-320 re-encode); the download form should hint at this when a
Bandcamp URL is detected.

## URL → mode mapping

| URL shape | Mode |
|---|---|
| `<artist>.bandcamp.com/track/<slug>` | `single` |
| `<artist>.bandcamp.com/album/<slug>` | `album` |
| `<artist>.bandcamp.com` / `…/music` | `artist` |
| custom artist domains (CNAME to Bandcamp) | **rejected** (host-based detection only, this iteration) |

## Metadata characteristics (easier than YouTube/SoundCloud)

- yt-dlp's Bandcamp extractor delivers clean structured fields: `artist`, `album`,
  `track`, `track_number`, release date — the tag chain gets good input without
  repair tricks.
- Cover art is square by design → `_square_crop_jpeg` is a natural no-op.
- Artist pages list albums + standalone tracks via yt-dlp's Bandcamp user/`/music`
  extraction — enumeration is flat and cheap compared to YT Music `/releases`.
- Paid-only/streaming-disabled tracks yield no downloadable format → skip + count
  into the existing partial-delivery surfacing (same pattern as 06's preview skip).

## Scope

**In:**

- `SourceSpec` registration: host matcher `*.bandcamp.com` (subdomain = artist),
  no extractor-args, no cookies/POT, `supports_artist=True`,
  `trust_uploader_as_artist=True` (the page owner is the artist in the overwhelming
  case; labels selling third-party releases carry proper `artist` fields which take
  precedence — same credit-tag-first order as 06).
- Mode suggestion per the table above.
- Per-source artist enumerator (the dispatch point exists after 06; if 11 lands
  before 06, create it here — whichever is first).
- Quality hint in the download form when a Bandcamp URL is detected.
- Bandcamp album URLs valid as `PlaylistSubscription`s (works for free; albums
  rarely change — the UI shouldn't push it).

**Out:**

- Purchased-collection downloads (needs login/cookies — different trust model,
  revisit only if requested).
- Custom artist domains.
- Bandcamp search.

## Acceptance criteria

1. A free Bandcamp **track** downloads as `single`, correctly tagged, square cover.
2. An **album** downloads as one album folder with track numbers; a
   streaming-disabled track in it is skipped and surfaces as partial delivery.
3. An **artist page** enumerates albums + tracks and runs with `own_artist`
   crediting + library dedup like other artist runs.
4. The quality hint appears for Bandcamp URLs and nowhere else.
5. YouTube (and, if merged, SoundCloud) snapshot/parity tests pass unchanged.
6. i18n complete (de + en); suite green.
