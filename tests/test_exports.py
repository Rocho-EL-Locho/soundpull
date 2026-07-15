"""Per-user export & backup (roadmap 17).

Exercised against a real in-memory SQLite session (no network, no NiceGUI render). Covers the
exact CSV/JSON content (BOM, quoting, umlauts, the feature-12 `artist,title` header), the
secret-exclusion guarantee (allowlist + a deny-scan), and the type-checked, secret-safe import
including a hostile payload.
"""
import csv
import io
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import app.models  # noqa: F401 — registers tables
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db
from app import exports
from app.models import DownloadHistory, ServerTrack, UserSettings


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def scope():
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        finally:
            sess.close()

    monkeypatch.setattr(app.db, "session_scope", scope)
    return scope


def _seed_user(scope, **overrides):
    with scope() as s:
        us = UserSettings(user_id=1, default_genre="Jazz", language="en", webdav_url="https://d",
                          webdav_username="alice")
        us.webdav_password_enc = "SEKRET"
        us.notify_smtp_password_enc = "SMTPPW"
        us.notify_ntfy_token_enc = "TOK"
        us.youtube_cookies_enc = "COOKIE"
        for k, v in overrides.items():
            setattr(us, k, v)
        s.add(us)


# --- library manifest ------------------------------------------------------

def test_library_csv_content_bom_header_quoting(env):
    with env() as s:
        s.add(ServerTrack(user_id=1, artist_norm="burial", title_norm="archängel",
                          rel_path="Burial/Untrue/05 - Archängel, Pt. 2.mp3"))
        s.add(ServerTrack(user_id=1, artist_norm="x", title_norm="seed", rel_path=None))
    out = exports.library_manifest_csv(1)

    assert out.startswith("﻿")                       # UTF-8 BOM present
    body = out[1:]
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["artist", "title", "album", "rel_path"]   # feature-12 contract
    data = {r[1]: r for r in rows[1:]}
    assert data["Archängel, Pt. 2"][0] == "Burial"        # comma-in-title quoted correctly
    assert data["Archängel, Pt. 2"][2] == "Untrue"
    assert data["seed"] == ["x", "seed", "", ""]          # rel_path=None seed falls back cleanly


def test_library_csv_neutralizes_formula_injection(env):
    with env() as s:  # rel_path=None → artist/title come straight from the (attacker) tags
        s.add(ServerTrack(user_id=1, artist_norm="x", title_norm="=cmd|' /C calc'!A0",
                          rel_path=None))
    rows = list(csv.reader(io.StringIO(exports.library_manifest_csv(1)[1:])))
    assert rows[1][0] == "x"                        # safe cell untouched
    assert rows[1][1] == "'=cmd|' /C calc'!A0"      # formula-leading title neutralized with a quote


def test_csv_safe_guards_all_formula_leads():
    for lead in ("=", "+", "-", "@", "\t", "\r"):
        assert exports._csv_safe(f"{lead}x") == f"'{lead}x"
    assert exports._csv_safe("normal") == "normal"
    assert exports._csv_safe(None) == ""
    assert exports._csv_safe(5) == "5"


def test_library_row_count_matches_index(env):
    with env() as s:
        for i in range(5):
            s.add(ServerTrack(user_id=1, artist_norm=f"a{i}", title_norm=f"t{i}",
                              rel_path=f"A{i}/Alb/{i}.mp3"))
        s.add(ServerTrack(user_id=2, artist_norm="other", title_norm="x", rel_path="Z/z/z.mp3"))
    rows = json.loads(exports.library_manifest_json(1))
    assert len(rows) == 5                                  # only this user's tracks


def test_empty_user_produces_valid_empty_exports(env):
    _seed_user(env)
    csv_out = exports.library_manifest_csv(1)
    assert csv_out[1:].strip() == "artist,title,album,rel_path"   # header only
    assert json.loads(exports.library_manifest_json(1)) == []
    assert exports.history_csv(1)[1:].startswith("id,url,mode")


# --- history ---------------------------------------------------------------

def test_history_csv_umlauts_and_columns(env):
    with env() as s:
        s.add(DownloadHistory(id="j1", user_id=1, url="u", genre="Jazz", mode="album",
                              destination_type="webdav", artist="Björk", album="Homogénic",
                              phase="done", created_at=datetime(2026, 7, 1, tzinfo=timezone.utc)))
    out = exports.history_csv(1)
    assert out.startswith("﻿")
    rows = list(csv.reader(io.StringIO(out[1:])))
    assert rows[0][:4] == ["id", "url", "mode", "genre"]
    assert rows[1][6] == "Björk" and rows[1][7] == "Homogénic"


# --- settings export (secret exclusion) ------------------------------------

def test_settings_json_excludes_all_secrets(env):
    _seed_user(env)
    out = exports.settings_json(1)
    data = json.loads(out)
    # Allowlist only, and none of the secret material leaks (deny-scan as a second net).
    assert set(data).issubset(set(exports._FIELD_KINDS))
    assert not any(k.endswith("_enc") or "password" in k or "token" in k or "cookie" in k
                   for k in data)
    for secret in ("SEKRET", "SMTPPW", "TOK", "COOKIE"):
        assert secret not in out
    assert data["default_genre"] == "Jazz" and data["webdav_username"] == "alice"


def test_settings_json_has_no_secret_shaped_field_even_as_deny_check(env):
    _seed_user(env)
    # Future-proofing: the exportable allowlist itself must never contain a secret-shaped name.
    assert not any(f.endswith("_enc") or "password" in f or "token" in f or "cookie" in f
                   for f in exports._FIELD_KINDS)


# --- settings import -------------------------------------------------------

def test_apply_settings_applies_valid_skips_invalid(env):
    _seed_user(env)
    payload = json.dumps({
        "default_genre": "Rock",           # valid str
        "trash_retention_days": -5,        # valid int, clamped to 0
        "tag_cover": "nope",               # wrong type → skipped
        "bogus_key": 1,                    # unknown → skipped
        "notify_smtp_port": 465,           # valid int
    })
    res = exports.apply_settings_json(1, payload)

    assert set(res.applied) == {"default_genre", "trash_retention_days", "notify_smtp_port"}
    assert set(res.skipped) == {"tag_cover", "bogus_key"}
    with env() as s:
        us = s.exec(select(UserSettings).where(UserSettings.user_id == 1)).first()
        assert us.default_genre == "Rock"
        assert us.trash_retention_days == 0        # negative clamped
        assert us.notify_smtp_port == 465


def test_apply_settings_never_writes_secrets_hostile_payload(env):
    _seed_user(env)
    payload = json.dumps({
        "webdav_password_enc": "HACKED",
        "notify_smtp_password_enc": "HACKED",
        "youtube_cookies_enc": "HACKED",
        "notify_ntfy_token_enc": "HACKED",
        "default_mode": "single",          # one legit field so `applied` is non-empty
    })
    res = exports.apply_settings_json(1, payload)

    assert res.applied == ["default_mode"]
    assert set(res.skipped) == {"webdav_password_enc", "notify_smtp_password_enc",
                                "youtube_cookies_enc", "notify_ntfy_token_enc"}
    with env() as s:
        us = s.exec(select(UserSettings).where(UserSettings.user_id == 1)).first()
        assert us.webdav_password_enc == "SEKRET"          # untouched
        assert us.notify_smtp_password_enc == "SMTPPW"
        assert us.youtube_cookies_enc == "COOKIE"
        assert us.notify_ntfy_token_enc == "TOK"
        assert us.default_mode == "single"


def test_apply_settings_rejects_non_object(env):
    _seed_user(env)
    assert exports.apply_settings_json(1, "[]").errors
    assert exports.apply_settings_json(1, "not json").errors
