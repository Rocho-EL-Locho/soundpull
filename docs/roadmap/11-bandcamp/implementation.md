# 11 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names. **Read feature 06's
implementation first** — this is the same shape with fewer quirks; reuse every
dispatch point it created instead of adding parallel ones.

## Touch points

| File | Change |
|---|---|
| `app/sources.py` | register `BANDCAMP` spec |
| `app/pipeline.py` | `_enumerate_artist_bandcamp`; verify probe fields map cleanly |
| `app/pages/index.py` | quality hint on Bandcamp detection |
| `app/i18n.py` | hint + skip-warning keys (de + en) |
| `tests/test_sources.py`, `tests/test_bandcamp.py` (new) | see Testing |

## Step plan

1. **Registry entry** (`app/sources.py`):

   ```python
   BANDCAMP = SourceSpec(
       key="bandcamp", label="Bandcamp",
       extractor_args=None,
       supports_cookies=False, supports_pot=False,
       supports_artist=True,
       cover_square_crop=True,            # no-op on square art
       trust_uploader_as_artist=True,
       matches=_match_bandcamp,           # host endswith .bandcamp.com, subdomain != www
       suggest_mode=_suggest_bc_mode,     # /track/ → single, /album/ → album, else artist
   )
   ```

2. **Flag derivation**: nothing new — `_apply_source` (02) already strips the
   `youtube:` extractor-args for non-YouTube sources.

3. **Artist enumeration**: `_enumerate_artist_bandcamp(url)` — normalize to
   `https://<sub>.bandcamp.com/music`, flat-probe with the same probe-opts style as
   the other enumerators; entries are album/track URLs. Map to the release-dict
   shape `run_artist_download` expects (one release per album URL; loose tracks as
   single-track releases). Verify against a real artist page early — yt-dlp's
   Bandcamp weekly/user extractors have shifted names across versions; the pinned
   yt-dlp version in `pyproject.toml` is the reference (check
   `yt_dlp/extractor/bandcamp.py` of the pinned version, don't trust memory).

4. **Probe field mapping**: run `_probe_meta` against a real track/album and confirm
   `artist`/`album`/`track`/`track_number` arrive as expected by the tag chain;
   Bandcamp titles sometimes still carry `Artist - Title` — the artist-mode repair
   helpers (`_repair_broken_title` etc.) apply unchanged via the 06 flag.

5. **Unavailable tracks**: streaming-disabled entries surface with no formats at
   probe/download time — ensure they flow into `expected`/`failed` accounting
   (`Result.failed_count` → `jobs.partial_delivery`) rather than aborting the album;
   follow whatever mechanism 06 built for previews (shared code path, not a copy).

6. **UI hint** (`app/pages/index.py`): when `detect_source(url).key == "bandcamp"`,
   show a small caption under the format select (i18n key
   `download.bandcamp_quality_hint`) recommending `original`. Purely informational —
   no forced format change.

## Testing

- `tests/test_sources.py`: host matching (`artist.bandcamp.com` yes,
  `www.bandcamp.com` root pages no-artist handling, unrelated hosts no),
  mode-suggestion table.
- `tests/test_bandcamp.py` (offline, fake probe payloads): enumeration mapping
  (albums + loose tracks, no duplicates), unavailable-track accounting, probe-field
  mapping into the tag chain's expected keys.
- Parity: YouTube snapshots unchanged (existing tests).
- **Manual verification (mandatory, document in the PR):** one free track, one album
  (ideally with one unavailable track), one small artist page; check tags/cover in
  Navidrome; re-run to confirm dedup.

## Definition of done

Acceptance criteria pass; manual matrix done; suite green; version bumped; PR.
