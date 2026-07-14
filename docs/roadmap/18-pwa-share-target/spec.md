# 18 — PWA + share target

**Phase:** 5 — Integrate · **Effort:** S–M · **Depends on:** — · **Issue:** —

## Goal

Make Soundpull installable as a **Progressive Web App** and register it as an
Android **share target**: in the YouTube Music app (or any browser) hit
"Share → Soundpull" and the download page opens with the URL pre-filled. This
replaces the bookmarklet on mobile, where bookmarklets are painful.

## Current state

- The app is a plain website; the bookmarklet (settings page) is the only
  quick-capture path, and it only works in desktop-style browsers on the YT Music
  website — not from the native app's share sheet.
- The stack already meets PWA preconditions: HTTPS via Traefik, single app shell.

## How it works (all standard web platform)

1. **Web app manifest** (`manifest.webmanifest`): name, icons, `display:
   standalone`, theme colors matching the glass theme, and a `share_target` entry:

   ```json
   "share_target": {
     "action": "/",
     "method": "GET",
     "params": { "url": "url", "text": "text", "title": "title" }
   }
   ```

   Android share sheets put the shared link in `url` or (commonly, e.g. from the
   YT Music app) inside `text` — both must be parsed.
2. **Minimal service worker** — required for installability; network-first
   passthrough (NO offline caching of app pages: the app is useless offline and a
   stale-cache NiceGUI shell causes websocket weirdness — keep the SW as dumb as
   possible).
3. **Index page** reads the `url` / `text` query params, extracts the first
   http(s) URL, pre-fills the input (mode auto-suggestion from feature 02 fires if
   merged), and cleans the query string from the address bar.

## Login interaction (must be verified, not assumed)

A share opens `/?url=…`; an expired session redirects to `/login` → OIDC → back.
The post-login redirect must **preserve the original query string** — check how
`AuthMiddleware`/the login flow in `app/auth.py` stores the intended target and fix
it to carry query params if it doesn't. This also fixes deep-linking in general.

## Scope

**In:** manifest + icons (generate a simple recognizable icon set from a text/logo
mark — 192/512 px + maskable variant), minimal service worker, head wiring
(`ui.add_head_html` in the shell), query-param prefill, login-redirect query
preservation, an "Install app" hint blurb on the settings page (replacing nothing —
the bookmarklet section stays).

**Out:**

- Offline functionality, caching, push notifications via the SW (ntfy covers push).
- iOS share-target (Safari doesn't support `share_target`; iOS users keep the
  bookmarklet / manual paste — say so honestly in the settings hint).
- Any change to the download flow itself.

## Acceptance criteria

1. Chrome/Android offers installation (Lighthouse PWA installability checks pass);
   the installed app opens in standalone mode with correct icon/name/colors.
2. Sharing a YT Music link from the native Android app to Soundpull lands on the
   download page with the URL pre-filled — including when the link arrives in the
   `text` param wrapped in prose.
3. Sharing while logged out: after OIDC login the URL is still pre-filled.
4. The service worker never serves a stale app shell (update flow verified: deploy
   a new version, reload gets it).
5. Desktop browsing is completely unaffected; no console errors from the SW.
6. i18n complete for new strings (de + en); suite green.
