# Soundpull

[![Tests](https://github.com/Rocho-EL-Locho/soundpull/actions/workflows/ci.yml/badge.svg)](https://github.com/Rocho-EL-Locho/soundpull/actions/workflows/ci.yml)
[![Build & publish](https://github.com/Rocho-EL-Locho/soundpull/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Rocho-EL-Locho/soundpull/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)

**Soundpull turns YouTube Music links into properly tagged MP3s.**

Paste a link to an album or a single, and Soundpull downloads it in high quality,
cleans up the metadata (artist, album artist, title, genre, square cover art) so it
looks right in music servers like [Navidrome](https://www.navidrome.org/), and hands
it to you either as a **ZIP download in your browser** or by uploading it **straight
to your own WebDAV storage**.

It's a small, self-hosted web app meant to run on your own server. Access is protected
by **single sign-on (authentik / OIDC)**, and every user gets their own profile, default
settings and download history.

## Screenshots

| Download | Settings |
|:--:|:--:|
| ![Download page](docs/screenshots/download.png) | ![Settings page](docs/screenshots/settings.png) |

## Features

- 🎵 **Albums & singles** — paste any YouTube Music URL
- 🏷️ **Clean, consistent tags** — feat. artists, album artist, title cleanup, genre and
  square cover art, tuned for Navidrome (and compatible with most players)
- 📦 **Flexible delivery** — download as a ZIP in the browser, or upload directly to a
  WebDAV target you pick from a built-in folder browser
- 🔐 **Protected** — login via authentik (OIDC); optionally restrict to a group
- 👤 **Per-user** — personal defaults, WebDAV credentials (encrypted) and history
- 📊 **Live progress** — watch each download move through its stages in real time
- 🔖 **Bookmarklet** — one click on a YouTube Music page opens Soundpull with the URL filled in

## How it works

```
Browser ──HTTPS──▶ Reverse proxy ──▶ Soundpull (web app)
                                       ├─ login via authentik (OIDC)
                                       ├─ per-user profiles & history
                                       └─ yt-dlp ─▶ tag cleanup ─▶ ZIP download | WebDAV upload
```

Under the hood it drives [yt-dlp](https://github.com/yt-dlp/yt-dlp) for the download and a
dedicated tagging step for the metadata. Built with [NiceGUI](https://nicegui.io/) (Python).

## Quick start (Docker)

1. Create an **OAuth2/OIDC application** in authentik with redirect URI
   `https://<your-host>/auth/callback` and scopes `openid email profile`.
2. Configure the app:
   ```bash
   cp .env.example .env
   # then fill in the values — see Configuration below
   ```
3. Adjust the host rule / TLS resolver in `docker-compose.yml` and start it:
   ```bash
   docker compose up -d --build
   ```

### Run locally (no authentik)

```bash
python -m venv .venv && .venv/bin/pip install .
.venv/bin/python -m app.main   # http://localhost:8080
```

If the `OIDC_*` variables are unset, login falls back to a local **dev user** so you can
try the UI without an authentik instance.

## Configuration

Set via environment / `.env` (see `.env.example`):

| Variable | Purpose |
|---|---|
| `APP_BASE_URL` | Public URL of the app (redirect URIs, bookmarklet) |
| `SESSION_SECRET` | Signs the session cookie |
| `FERNET_KEY` | Encrypts stored WebDAV passwords at rest |
| `OIDC_DISCOVERY_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI` | authentik OIDC |
| `OIDC_ALLOWED_GROUP` | *(optional)* restrict access to a group |
| `LOCAL_MUSIC_ROOT` | Staging/temp directory for downloads |
| `MAX_CONCURRENT_DOWNLOADS` | How many downloads run at once |

## Usage

1. Open the app and sign in.
2. Paste a YouTube Music URL (or use the bookmarklet from **Settings**).
3. Pick genre, album/single, and the destination (browser ZIP or WebDAV).
4. Start — follow the live progress; the ZIP download starts automatically when done.

In **Settings** you set your defaults and, for WebDAV, connect and browse to a target
folder. Your WebDAV password is stored encrypted.

## Tech stack

NiceGUI (FastAPI) · Authlib (OIDC) · SQLModel + SQLite · yt-dlp · mutagen · webdav4 ·
Docker + Traefik.

## License

[MIT](LICENSE)
