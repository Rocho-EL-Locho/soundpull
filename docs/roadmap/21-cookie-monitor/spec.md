# 21 — Cookie health monitor

**Phase:** 6 — Resilience · **Effort:** S · **Depends on:** — · **Issue:** —

## Goal

Detect an **expired/invalid YouTube cookie before the user does.** Today a dead
cookie degrades silently: age-restricted tracks start failing (403 on mweb) or fall
to worse formats, downloads still report "done, partial", and the user only notices
when an album is mysteriously incomplete. A periodic validity probe plus a
notification/banner turns that into an actionable heads-up.

## Current state

- The per-user cookie (`UserSettings.youtube_cookies_enc`, Fernet-encrypted) feeds
  `_apply_cookie_policy` — with a cookie, mweb+PO-token carries age-restricted
  downloads; without (or with a dead one), those tracks 403 and land in
  `failed_tracks`.
- Nothing validates the cookie proactively; the notification system (issue #42) and
  the scheduler exist and are the natural carriers.

## Detection design

- **Probe**: metadata-only yt-dlp extraction (`skip_download`, socket-timeout
  applied like every opts dict) of a known **age-restricted** video using the
  cookie — exactly the path that breaks when the cookie dies. Configurable test
  video id (`env COOKIE_PROBE_VIDEO_ID`, sane default) since any single video can
  disappear.
- **Judgement**: extraction ok → cookie healthy. Age-gate/login error → cookie
  invalid. Network/other errors → *unknown*, *not* a failure (no false alarms from
  a flaky minute).
- **Debounce**: state flips to "invalid" only after **2 consecutive** invalid
  probes; a notification fires on the healthy→invalid transition ONLY (no daily
  nagging), and once on recovery (invalid→healthy, informational).
- **Cadence**: daily, via the existing scheduler tick (users with a stored cookie
  only). Also re-probe immediately when the user saves a new cookie (instant
  feedback on the settings page).

## Scope

**In:**

- Probe + state machine; `UserSettings` fields for status
  (`cookie_status: unknown|ok|invalid`, `cookie_checked_at`, consecutive-failure
  counter).
- New notification toggle `notify_cookie_invalid` (default **on** when any channel
  is configured — this is exactly what notifications are for; still a visible,
  disableable toggle) using the existing channels/payload rules (no cookie content
  in any payload, ever).
- UI surfacing: status chip next to the cookie field on the settings page
  ("checked 3h ago — OK"); a dismissible warning banner on the download page while
  status is `invalid`.
- Save-time probe with inline result.

**Out:**

- Auto-refreshing/re-acquiring cookies (impossible server-side by design).
- Probing SoundCloud/Bandcamp auth (no cookies there in this roadmap).
- Multi-cookie management.

## Acceptance criteria

1. A user with a valid cookie: daily probe passes, status `ok`, no notifications.
2. Cookie invalidated (simulate: probe returns age-gate error twice) → status
   `invalid`, ONE notification via configured channels, banner appears; a third
   failing probe sends nothing further.
3. Saving a fresh working cookie → immediate probe, status `ok`, recovery notice,
   banner gone.
4. Probe errors of network type never flip the status or notify.
5. The probe never downloads media (metadata only), runs off the event loop, and
   its yt-dlp opts go through the same `_apply_*` helpers as real runs (cookie,
   socket timeout, extractor args — it must test the REAL path).
6. No cookie material in any notification payload or log line (existing no-secret
   test style extended).
7. i18n complete (de + en); suite green; users without a cookie are never probed.
