# 02 — Multi-source architecture (source registry + URL intelligence)

**Phase:** 1 — Foundation · **Effort:** M · **Depends on:** — · **Issue:** — (enables #30)

## Goal

Decouple the pipeline from YouTube so additional sources (SoundCloud in feature 06,
later Bandcamp/Mixcloud/…) plug in as **registry entries** instead of scattered edits.
Bonus UX: **URL intelligence** — when the user pastes a URL, detect the source and
pre-select the most likely mode (album/single/playlist/artist).

## Current state (everything is YouTube-shaped)

- `is_supported_url` (`app/pipeline.py:71`) hard-codes `_YOUTUBE_HOSTS` (L39) /
  `*.youtube.com`; enforced in the UI at `app/pages/index.py` (~L218) and
  `app/pages/subscriptions.py` (~L161).
- `EXTRACTOR_ARGS = "youtube:player_client=android_vr,mweb"` (`pipeline.py:114`) is
  baked into `_ALBUM_FLAGS` / `_SINGLE_FLAGS` and injected into probes via
  `_extractor_args()`.
- Cookie policy (`_apply_cookie_policy`) uses the per-user **YouTube** cookie;
  PO-token plumbing (`_apply_pot_provider`) is YouTube-GVS-specific.
- `enumerate_artist` resolves YT-Music `/releases` tabs; `pick_square_cover` prefers
  signed `sqp=` YouTube thumbnail URLs.
- Mode is purely user-selected via the toggle; no URL sniffing exists.

## Scope

**In:**

- New `app/sources.py` with a frozen `SourceSpec` per source and a registry:
  - source key + display name, host matcher
  - per-source extractor-args (or none), cookie support flag, PO-token flag
  - artist-mode support flag (+ hook for a per-source artist enumerator, used by 06)
  - cover strategy flag (square-crop needed or artwork already square)
  - `suggest_mode(url)` — best-guess mode from URL shape
- `detect_source(url) -> SourceSpec | None`; `is_supported_url` becomes a thin
  wrapper (keep the name — three call sites stay untouched).
- Pipeline reads source-specific bits (extractor args, cookie/POT applicability)
  from the detected source instead of module constants — with **YouTube as the only
  registered source** in this feature, so behavior is 100 % unchanged.
- UI: on URL input change in `app/pages/index.py`, pre-select the suggested mode
  (only auto-adjust while the user hasn't manually overridden the toggle for the
  current URL).

**Out:**

- Actually registering SoundCloud (that is feature 06).
- Any change to the frozen flag lists' YouTube content, tag chain, or
  `fix_music_tags.py`.

## ⚠️ Parity constraint (critical)

This is a refactor **around** the parity mechanism, not of it. The YouTube path must
produce **byte-identical yt-dlp options**: `_ALBUM_FLAGS` / `_SINGLE_FLAGS` keep their
exact content for YouTube; non-YouTube sources get a *derived* list via a transform
(the `_apply_audio_format()` no-op-by-default precedent). The existing options
snapshot in `tests/test_pipeline.py` must pass unchanged, and a new test must assert
the YouTube-derived list is `==` the original.

## Mode suggestion table (YouTube)

| URL shape | Suggested mode |
|---|---|
| `…?list=OLAK5uy_…` (album playlist id) | `album` |
| `watch?v=…` without `list=` | `single` |
| `playlist?list=PL…` / `RD…` / other list ids | `playlist` |
| `/channel/…`, `/@handle`, `music.youtube.com/channel/…` | `artist` |
| anything else | no suggestion (keep current toggle) |

## Acceptance criteria

1. All existing tests green, **including the pipeline options snapshot** — proof the
   YouTube path is unchanged.
2. `detect_source` correctly classifies the URL table above (unit-tested) and returns
   `None` for unknown hosts; unknown hosts are still rejected in the UI with the
   existing error message.
3. Adding a hypothetical new source requires only a registry entry (demonstrated by a
   test that registers a dummy source and round-trips detection + flag derivation).
4. Pasting an album/watch/playlist/channel URL pre-selects the right mode toggle; a
   manual toggle choice is not fought by the auto-suggestion.
5. No new i18n keys missing their counterpart language.
