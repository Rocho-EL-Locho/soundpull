# 09 — Configurable tagging rules (multi-artist separator)

**Phase:** 4 — Comfort · **Effort:** S–M · **Depends on:** — · **Issue:** #8

## Goal

Let the user choose the **multi-artist separator** written into artist tags — today
hard-coded to `" / "` (`Primary / Feat`). Other players/servers prefer `"; "`
(Picard/ID3v2.4 style) or `", "`. Issue #8's ask.

## ⚠️ This feature lives inside the parity danger zone

`app/fix_music_tags.py` is frozen; the `" / "` convention is baked into its rules AND
into consumers across the app. The design principle is the established one: **the
default is a byte-identical no-op**; the new behavior only activates when the user
picks a non-default separator.

## Everywhere `" / "` is currently assumed (all must be audited)

- `fix_music_tags.py` — joins feat/comma artists with `" / "`; album-artist fallback
  takes the first `" / "` segment.
- `library_index._primary_artist` — splits on `/` (and `,`) to build `track_key`.
- `app/lyrics.py` `write_lrc_for` — primary artist = first `" / "` segment.
- `pipeline._prefix_artists` (collab handling) — produces `"A / B"` artist values.
- Navidrome itself — parses multi-artist strings; the user's server config must match
  whatever separator they pick (mention in the settings hint).

## Scope

**In:**

- `UserSettings.artist_separator` enum-keyed setting: `slash` (default, `" / "`),
  `semicolon` (`"; "`), `comma` (`", "`). Settings UI select in the metadata card
  with a hint that this affects only **newly tagged** downloads and must match the
  media server's expectation.
- Thread the separator through the tagging write path as an **optional parameter
  defaulting to `" / "`** (the precedent: the parity-safe `album_artist` fallback
  extension). Same value must reach: `fix_music_tags` write path (all three format
  adapters), `_prefix_artists`, lyrics primary-artist split, and
  `library_index`'s key normalization (splitting must accept **all** known
  separators regardless of setting — see below).
- `track_key` robustness: `_primary_artist` splits on the union `/`, `;`, `,`
  always — so a library mixing separators (user changed the setting mid-life) still
  dedups correctly. **Important:** `&` and ` x ` stay non-separators (real band
  names — "Simon & Garfunkel" rule).
- A migration story for the *existing* library is explicitly **rewrite-on-demand
  only**: feature 05's deep-check could later gain a "re-write artist separator"
  fix; not part of this feature.

**Out:**

- Any change to *which* artists are detected/split (feat rules, `&`/`x`/`und`
  non-splitting) — only the join string is configurable.
- Toggling feat-normalization (already covered by the existing `tag_feat_artist`
  toggle).
- ID3v2.4 multi-value TPE1 frames (the app writes ID3v2.3 single-string frames —
  stays).

## Acceptance criteria

1. **Default `slash`: byte-identical output.** The full fix_music_tags test suite and
   pipeline snapshots pass unchanged; a before/after file diff on a real download is
   identical.
2. With `semicolon`, a feat track tags as `Primary; Feat` in MP3, M4A **and** Opus;
   album-artist stays the primary artist alone; the title feat-strip behavior is
   unchanged.
3. `track_key("A; B", …) == track_key("A / B", …) == track_key("A, B", …)` — dedup
   and lyrics lookups keep working across separator styles.
4. `.lrc` fetching uses the primary artist correctly for all three separators.
5. Settings UI shows the option with a clear hint; i18n complete (de + en).
6. Existing genres/covers/other frames untouched; suite green.
