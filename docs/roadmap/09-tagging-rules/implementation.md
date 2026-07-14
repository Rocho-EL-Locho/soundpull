# 09 — Implementation plan

Line numbers are approximate for v0.10.0; trust function names. **This is the most
parity-sensitive feature in the roadmap — smallest possible diff, test-first.**

## Touch points

| File | Change |
|---|---|
| `app/models.py` | `UserSettings.artist_separator: str = Field(default="slash")` |
| `app/fix_music_tags.py` | optional `artist_separator=" / "` parameter threaded into the write path — **no rule changes** |
| `app/pipeline.py` | pass the user's separator into tagging + `_prefix_artists` |
| `app/lyrics.py` | primary-artist split accepts all separators |
| `app/library_index.py` | `_primary_artist` splits on `/`, `;`, `,` |
| `app/jobs.py` | thread setting from `UserSettings` into the pipeline call (follow how `tag_options` flows today) |
| `app/pages/settings.py` | select in the metadata card + hint |
| `app/i18n.py` | keys (de + en) |
| `tests/test_fix_music_tags.py`, `tests/test_library_index.py`, `tests/test_lyrics.py` | see Testing |

## Approach decision

Two options were considered:

- **(A) Post-pass**: rewrite the artist/album-artist frames after `fix_music_tags`
  ran — keeps the frozen module 100 % untouched, but duplicates per-format frame
  handling for 3 formats and risks divergence (encodings, ID3 versions).
- **(B) Optional parameter** into the frozen module's write path, default `" / "` →
  default path bit-identical. This follows the module's own precedent (the
  parity-safe `album_artist` fallback), touches each join/split point with a
  one-line change, and is guarded by the byte-identity tests.

**Choose (B).** The frozen-ness protects the *rules*; a default-preserving parameter
is an established extension pattern there.

## Step plan

1. **Write the guard tests first**: extend `tests/test_fix_music_tags.py` with an
   explicit byte-identity assertion — process a fixture file with
   `artist_separator=" / "` (and with the parameter omitted) and assert outputs are
   byte-equal to today's expected output. Only then start editing.
2. **`fix_music_tags.py`**: add `artist_separator: str = " / "` to `process_file` /
   `process_directory` / `process_tree` / `_normalized_tags` signatures. Replace the
   literal join `" / ".join(...)` at the (few) points where the final artist string
   is assembled, and the album-artist-fallback split, with the parameter. Grep for
   `" / "` in the module and account for **every** occurrence (join vs split vs
   display); splits should split on the *configured* separator when reading values
   this same run wrote, but on the legacy `" / "` too — simplest: split on any of
   the three separators (a small `_split_multi(value)` local helper).
3. **Mapping**: `SEPARATORS = {"slash": " / ", "semicolon": "; ", "comma": ", "}` in
   a neutral place (e.g. next to `AUDIO_FORMATS` in `pipeline.py` or in
   `models.py`-adjacent constants module — NOT in `fix_music_tags.py`).
4. **`pipeline.py`**: `run_download`/`run_artist_download` accept
   `artist_separator=" / "` and forward to every `process_directory`/`process_tree`
   call; `_prefix_artists`' `"A / B"` join uses it too. `_dedup_staged_tracks` /
   match-filter keys go through `track_key` (already separator-agnostic after step
   5) — verify by reading, don't assume.
5. **`library_index.py`**: `_primary_artist` splits on `/`, `;`, `,` (regex
   `re.split(r"\s*[/;,]\s*", …, 1)[0]`-style, preserving today's behavior for `/`
   and `,`). Existing tests must stay green.
6. **`app/lyrics.py`**: `write_lrc_for`'s primary-artist extraction → same
   multi-separator split (share the helper if import direction allows; lyrics
   already imports from fix_music_tags-adjacent code — check the import graph, else
   duplicate the 1-line regex with a comment).
7. **`jobs.py` + settings UI + i18n**: read `UserSettings.artist_separator`, map via
   `SEPARATORS`, pass into the pipeline calls (all three job paths: `_run`,
   `_run_artist`, `_run_sync`). Settings select + hint text.

## Testing

- Byte-identity guard (step 1) — the core deliverable.
- Per-format (MP3/M4A/Opus) semicolon + comma outputs: feat-in-title case,
  feat-in-artist case, comma-list case, collab `x` case (`_prefix_artists`).
- `track_key` equivalence across all three separators (issue-#8 regression test).
- Lyrics primary-artist extraction for all separators.
- i18n parity test green.

## Definition of done

Acceptance criteria pass — especially the byte-identity proof; a real download
re-verified with default settings (diff against a pre-change download of the same
album); suite green; version bumped; PR references issue #8 ("Closes #8").
