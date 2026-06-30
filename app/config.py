"""Application settings, loaded from environment / .env (see .env.example)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_base_url: str = "http://localhost:8080"
    session_secret: str = "dev-insecure-session-secret-change-me"
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

    @property
    def oidc_configured(self) -> bool:
        return bool(self.oidc_discovery_url and self.oidc_client_id and self.oidc_client_secret)


settings = Settings()
