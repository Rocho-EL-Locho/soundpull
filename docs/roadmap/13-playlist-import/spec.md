# 13 — Spotify / Apple Music playlist import

**Phase:** 3 — Grow · **Effort:** L · **Depends on:** 07, 12 · **Issue:** —

## Goal

Paste a **public Spotify or Apple Music playlist/album URL** — Soundpull reads the
track list, matches every track on YouTube Music (feature 12's engine), lets the user
review, downloads the confirmed tracks, and optionally recreates the playlist as an
`.m3u8` in the library. The migration feature for people moving from streaming to
self-hosting.

**What this is NOT:** downloading audio from Spotify/Apple (DRM — impossible and out
of bounds). Only the *metadata* (track list) is read; audio comes from YouTube Music
like every other download.

## Sources for the track list

- **Spotify** — official Web API via client-credentials flow: server-level
  `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` env vars (free developer app, no user
  OAuth needed for public playlists/albums). Without configured credentials the
  Spotify option is hidden and the settings/docs explain the two env vars.
  Dependency: `spotipy` (pinned) or plain `httpx` calls — decide in implementation
  (plain HTTP preferred: the token flow + two GET endpoints don't justify a
  dependency).
- **Apple Music** — no key-free API. Public playlist pages embed the track list as
  JSON in the HTML (`serialized-server-data` script tag). Parse best-effort, clearly
  marked **fragile** in code and UI (a markup change breaks it soft: error toast,
  nothing else affected). No dependency, just `httpx` + `json`.

Both parsers output the same neutral shape: `list[(artist, title, album?)]` → fed
into `matching.match_all` (12). Everything downstream (review, batch job, dedup) is
feature 12 unchanged.

## Playlist recreation (second milestone within this feature)

After the batch download completes, optionally write the playlist into the library
the way native playlists work (issue #11/#31 machinery):

- Folder `<playlist name> [import-<hash>]/` containing only the `.m3u8` — every line
  a **cross-folder relative reference** to the delivered single's `rel_path`
  (`posixpath.relpath`, exactly the existing reference mechanism for dedup-skipped
  playlist tracks). Tracks physically live in their artist/album folders; Navidrome
  imports the m3u and resolves the references.
- Tracks that were skipped as already-on-server reference their existing
  `rel_path` from the index — the imported playlist can point at music downloaded
  years ago.
- Unmatched/failed tracks are simply absent from the m3u (and listed in the job
  summary).

## Scope

**In:** URL detection (open.spotify.com playlist/album; music.apple.com playlist/
album), both parsers, review flow via 12, optional m3u recreation (checkbox in the
review step, default on, WebDAV destination only).

**Out:**

- Private playlists (needs user OAuth — explicitly later, if ever).
- Continuous playlist *sync* (a Spotify playlist as `PlaylistSubscription` — nice
  future extension; the import folder naming `[import-<hash>]` is chosen stable so a
  future sync could target it).
- Other services (Deezer, Tidal — the parser interface makes them cheap follow-ups).

## SSRF / trust

Only two fixed host families are ever fetched (`api.spotify.com` +
`accounts.spotify.com`; `music.apple.com`) — validate the pasted URL's host against
exactly these before any request (same philosophy as `_valid_http_url`).

## Acceptance criteria

1. A public Spotify playlist URL yields its full track list (pagination handled —
   playlists > 100 tracks) into the feature-12 review table.
2. An Apple Music public playlist URL does the same; when parsing fails, the user
   gets a translated error and nothing else breaks.
3. Confirmed tracks download as one batch job; already-in-library tracks are skipped
   but still referenced in the recreated m3u.
4. The recreated playlist appears in Navidrome with working entries pointing at the
   real artist/album files (no duplicated audio).
5. Missing Spotify credentials → Spotify option hidden + documented; Apple path
   works regardless.
6. No Spotify/Apple audio is ever fetched; only metadata endpoints are contacted;
   host validation enforced.
7. i18n complete (de + en); suite green.
