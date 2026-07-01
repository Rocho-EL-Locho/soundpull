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

    # Optional SSRF guard: comma-separated host allowlist for WebDAV targets.
    # Empty = no restriction (any host the server can reach is allowed).
    webdav_allowed_hosts: str = ""

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


settings = Settings()
