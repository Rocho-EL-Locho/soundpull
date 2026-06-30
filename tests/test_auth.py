"""Open-redirect guard on the post-login redirect target."""
from app.auth import _safe_redirect_target


def test_allows_local_paths():
    assert _safe_redirect_target("/settings") == "/settings"
    assert _safe_redirect_target("/history?x=1") == "/history?x=1"


def test_blocks_open_redirects():
    assert _safe_redirect_target("//evil.com") == "/"
    assert _safe_redirect_target("/\\evil.com") == "/"        # browsers read \ as /
    assert _safe_redirect_target("https://evil.com") == "/"
    assert _safe_redirect_target("/ok\nLocation: x") == "/"   # control chars
    assert _safe_redirect_target(None) == "/"
    assert _safe_redirect_target("") == "/"
