# 19 — Navidrome integration

**Phase:** 5 — Integrate · **Effort:** S–M · **Depends on:** — (03 uses the deep links) · **Issue:** —

## Goal

Close the loop with the media server the whole app tags for: after a WebDAV
delivery, **trigger a Navidrome library scan** so new music is playable seconds
later instead of "whenever the next scheduled scan runs" — plus deep links from
Soundpull pages into the Navidrome UI.

## Current state

- Soundpull uploads to WebDAV and is done; Navidrome notices new files only on its
  own schedule. After a download the user waits or clicks "rescan" in Navidrome
  manually.
- Feature 03 sketched an optional `navidrome_base_url` for dumb UI links; this
  feature owns the setting properly and adds the authenticated API part.

## How (Subsonic API — stable and version-proof)

Navidrome implements the Subsonic REST API, including `rest/startScan`. Auth per
request: username + salted token (`t = md5(password + salt)`, `s = salt`) — the
standard Subsonic scheme; over HTTPS this is fine and avoids Navidrome-native JWT
session handling. Recommend a **dedicated Navidrome user** for Soundpull in the
settings hint (least privilege; scan trigger needs admin in Navidrome — say so).

## Scope

**In:**

- Per-user settings (new "Navidrome" card): base URL, username, password
  (Fernet-encrypted like the WebDAV password, `has_*` flag to the client), and a
  **"trigger scan after upload" toggle** (default off). "Test connection" button
  (`rest/ping`) like the notification test — operates on saved settings.
- **Scan trigger**: after a successful WebDAV delivery (manual download, artist
  run, interval sync), fire `rest/startScan` **best-effort** — the
  `_notify_safe` pattern: never fails or delays the job, logged either way. One
  trigger per job, after the terminal state is persisted (an artist run = one
  trigger at the end, not per release).
- **Deep links**: where feature 03/04/05 pages show albums/artists, an optional
  "open in Navidrome" link when the base URL is set (search-URL links, no API —
  as specced in 03; this feature just makes the setting real and shared).
- **SSRF guard**: base URL http(s)-only + reuse the same host-allowlist mechanism
  as WebDAV/notifications.

**Out:**

- Reading playback stats from Navidrome (listen counts for the stats page — a
  possible later extension, not now).
- Playlist push via API (the `.m3u8` auto-import already covers playlists).
- Any Navidrome-native (non-Subsonic) API usage.

## Acceptance criteria

1. With the toggle on, a WebDAV download is followed by exactly one `startScan`
   call; new tracks appear in Navidrome without manual action (manual verification).
2. Navidrome being down/misconfigured never affects the download result — job
   `done`, a log line records the failed trigger.
3. The password is encrypted at rest, never sent to the client; the test button
   reports ping success/failure with a translated message.
4. Interval syncs trigger the scan only when they actually delivered something
   (`new_track_count > 0`).
5. URL validation rejects non-http(s) and disallowed hosts.
6. i18n complete (de + en); suite green; zero pipeline/tagging changes.
