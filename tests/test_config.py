"""Deployment-awareness: dev login + WebDAV allowlist gating."""
from app.config import Settings

_OIDC = dict(oidc_discovery_url="https://auth/.well-known",
             oidc_client_id="id", oidc_client_secret="secret")


def _settings(**kw):
    # Explicit kwargs override env/.env, so OIDC is off unless a test opts in.
    base = dict(oidc_discovery_url="", oidc_client_id="", oidc_client_secret="")
    base.update(kw)
    return Settings(**base)


def test_local_deployment_detection():
    assert _settings(app_base_url="http://localhost:8080").is_local_deployment
    assert _settings(app_base_url="http://127.0.0.1:9000").is_local_deployment
    assert not _settings(app_base_url="https://soundpull.example.org").is_local_deployment
    # Fail safe: a scheme-less / unparseable URL is NOT local (no open dev login).
    assert not _settings(app_base_url="soundpull.example.org").is_local_deployment
    assert not _settings(app_base_url="").is_local_deployment


def test_dev_login_only_local_and_without_oidc():
    assert _settings(app_base_url="http://localhost:8080").dev_login_allowed
    # configured OIDC disables the dev login even locally
    assert not _settings(app_base_url="http://localhost:8080", **_OIDC).dev_login_allowed
    # a non-local deployment must never fall back to the dev login
    assert not _settings(app_base_url="https://soundpull.example.org").dev_login_allowed


def test_webdav_host_allowlist_parsing():
    s = _settings(webdav_allowed_hosts="Cloud.Example.org, dav.foo ")
    assert s.webdav_host_allowlist == {"cloud.example.org", "dav.foo"}
    assert _settings(webdav_allowed_hosts="").webdav_host_allowlist == set()


def test_max_playlist_items_default_and_override():
    # Playlist cap (issue #11): a sane default, overridable (0 = unlimited).
    assert _settings().max_playlist_items == 100
    assert _settings(max_playlist_items=0).max_playlist_items == 0
    assert _settings(max_playlist_items=25).max_playlist_items == 25
