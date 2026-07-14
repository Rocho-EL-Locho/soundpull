"""Application settings, loaded from environment / .env (see .env.example)."""
from __future__ import annotations

from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

# Shipped placeholder — refused in non-local deployments (see Settings.is_local_deployment).
DEFAULT_SESSION_SECRET = "dev-insecure-session-secret-change-me"

# Hosts that count as a local/dev deployment (enables the OIDC-less dev login).
# A missing/unparseable host is deliberately NOT local — that fails safe
# (production guards apply) when app_base_url lacks a scheme, e.g. "host.example".
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_base_url: str = "http://localhost:8080"
    session_secret: str = DEFAULT_SESSION_SECRET
    fernet_key: str = ""  # required for WebDAV password storage; see .env.example

    # OIDC / authentik
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid email profile"
    oidc_allowed_group: str | None = None
    oidc_post_logout_redirect: str | None = None

    # Storage
    database_url: str = "sqlite:///./data/app.db"
    local_music_root: str = "./downloads"
    max_concurrent_downloads: int = 2
    # Cap on tracks fetched from a single playlist (issue #11). 0 = unlimited.
    max_playlist_items: int = 100
    # Cap on the number of releases pulled for an artist download (issue #32). 0 = unlimited.
    max_artist_items: int = 0
    # Albums downloaded in parallel within one artist run (issue #32). Clamped to 1–4.
    max_artist_album_concurrency: int = 3

    # Resilience against transient YouTube throttling on multi-track runs (album /
    # playlist / artist). A back-to-back marathon of big downloads can get the IP
    # temporarily rate-limited; yt-dlp then silently skips the throttled tracks
    # (`ignoreerrors='only_download'`), so an album can come out partial while the job
    # still reports "done". These knobs make that recoverable and visible:
    #   - retry_passes: after the first pass, re-run up to this many more times, using a
    #     per-job download-archive so only the still-missing tracks are re-attempted.
    #   - retry_backoff_seconds: wait this long before each retry pass so a throttle can
    #     clear. 0 disables the wait.
    #   - sleep_requests_seconds: paced delay between yt-dlp HTTP requests on a multi-track
    #     run to AVOID tripping the throttle in the first place. 0 = off (fastest). A small
    #     value (e.g. 0.5) helps when pulling whole discographies.
    #   - socket_timeout_seconds: cap on yt-dlp's per-socket wait (issue #40). yt-dlp defaults
    #     to no timeout, so a STALLED (half-open) connection blocks the worker thread forever —
    #     with the default 2-worker pool, one stuck job halves throughput for everyone. A
    #     deadline turns a stall into a retryable error. 0/negative = yt-dlp's default (none).
    # All four only affect timing / which tracks are retried — never tag output (parity-safe).
    download_retry_passes: int = 2
    download_retry_backoff_seconds: float = 30.0
    download_sleep_requests_seconds: float = 0.0
    download_socket_timeout_seconds: float = 60.0

    # PO-token provider (issue: YouTube 403). YouTube now requires a GVS PO token
    # for most audio formats; without one the affected clients' format URLs return
    # HTTP 403. Point this at a running bgutil-ytdlp-pot-provider server (e.g. the
    # docker-compose sidecar: http://bgutil-provider:4416) and the bundled yt-dlp
    # plugin fetches tokens from it. Empty = disabled (the plugin stays idle and
    # yt-dlp falls back to token-free clients like android_vr, at reduced quality).
    pot_provider_base_url: str = ""

    # Playlist interval-sync (issue #21). `sync_enabled` is the master switch for the
    # background scheduler; `sync_tick_seconds` is how often it checks for due
    # subscriptions (the per-subscription cadence is `interval_hours`).
    sync_enabled: bool = True
    sync_tick_seconds: int = 60

    # Optional SSRF guard: comma-separated host allowlist for WebDAV targets.
    # Empty = no restriction (any host the server can reach is allowed).
    webdav_allowed_hosts: str = ""

    # Optional SSRF guard for notification targets (ntfy / webhook URLs, issue #42).
    # Comma-separated host allowlist; empty = no restriction. Same trust model as WebDAV:
    # each user configures their own notification endpoint.
    notify_allowed_hosts: str = ""

    @property
    def oidc_configured(self) -> bool:
        return bool(self.oidc_discovery_url and self.oidc_client_id and self.oidc_client_secret)

    @property
    def is_local_deployment(self) -> bool:
        """True when app_base_url points at localhost — gates dev-only behaviour."""
        return (urlparse(self.app_base_url).hostname or "") in _LOCAL_HOSTS

    @property
    def dev_login_allowed(self) -> bool:
        """OIDC-less auto-login is only safe on a local deployment."""
        return not self.oidc_configured and self.is_local_deployment

    @property
    def webdav_host_allowlist(self) -> set[str]:
        return {h.strip().lower() for h in self.webdav_allowed_hosts.split(",") if h.strip()}

    @property
    def notify_host_allowlist(self) -> set[str]:
        return {h.strip().lower() for h in self.notify_allowed_hosts.split(",") if h.strip()}


settings = Settings()
