"""Track-list parsing + matching (roadmap 12 batch import).

Offline: `search.search_songs` is monkeypatched, so no network. Covers the accepted line shapes,
the confidence scoring (shared normalization with `track_key`), and `match_all`'s candidate
selection / on-server flagging / progress.
"""
from contextlib import contextmanager

import app.models  # noqa: F401 — registers tables
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.db
from app import matching, search
from app.matching import ParsedLine, parse_lines, score
from app.models import ServerTrack, UserSettings
from app.search import SearchResult


def _song(title, artist, url="https://music.youtube.com/watch?v=x"):
    return SearchResult(kind="song", title=title, artist=artist, url=url,
                        browse_id=None, thumbnail=None)


# --- parse_lines -----------------------------------------------------------

def test_parse_dash_variants_and_first_separator():
    lines = parse_lines("Burial - Archangel\nAdele – Hello\nAr — Ti - with dash")
    assert (lines[0].artist, lines[0].title) == ("Burial", "Archangel")
    assert (lines[1].artist, lines[1].title) == ("Adele", "Hello")          # en dash
    # em dash is the first separator; the trailing ' - with dash' stays in the title
    assert (lines[2].artist, lines[2].title) == ("Ar", "Ti - with dash")


def test_parse_tab_separated():
    (line,) = parse_lines("The Beatles\tCome Together")
    assert (line.artist, line.title) == ("The Beatles", "Come Together")


def test_parse_csv_with_header_and_quoted_comma():
    lines = parse_lines('artist,title\nBurial,Archangel\n"Tyler, The Creator",EARFQUAKE')
    assert (lines[0].artist, lines[0].title) == ("Burial", "Archangel")
    assert (lines[1].artist, lines[1].title) == ("Tyler, The Creator", "EARFQUAKE")


def test_parse_unparseable_lines_are_reported_not_dropped():
    lines = parse_lines("Good - One\njustonefield\n\n   ")
    assert len(lines) == 2                       # blank lines ignored, junk kept
    assert lines[0].ok
    assert not lines[1].ok and lines[1].error == "import.parse_error"


def test_parse_respects_max_lines():
    text = "\n".join(f"A{i} - T{i}" for i in range(matching.MAX_LINES + 50))
    assert len(parse_lines(text)) == matching.MAX_LINES


# --- score -----------------------------------------------------------------

def test_score_exact_is_one():
    pl = ParsedLine(raw="x", artist="Burial", title="Archangel")
    assert score(pl, _song("Archangel", "Burial")) == pytest.approx(1.0)


def test_score_wrong_artist_is_low_even_with_exact_title():
    pl = ParsedLine(raw="x", artist="Burial", title="Archangel")
    assert score(pl, _song("Archangel", "Taylor Swift")) < 0.5   # min(artist,title) kills it


def test_score_feat_variant_is_high():
    pl = ParsedLine(raw="x", artist="Drake", title="Something")
    # "(feat. …)" is stripped by the shared _clean_title, so title still matches ~exactly
    assert score(pl, _song("Something (feat. Rihanna)", "Drake / Rihanna")) == pytest.approx(1.0)


def test_score_unparsed_line_is_zero():
    assert score(ParsedLine(raw="junk", error="import.parse_error"), _song("A", "B")) == 0.0


# --- match_all -------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(UserSettings(user_id=1))
        # Burial – Archangel is already in the library (for the on_server flag).
        s.add(ServerTrack(user_id=1, artist_norm="burial", title_norm="archangel",
                          rel_path="Burial/Untrue/05 - Archangel.mp3"))
        s.commit()

    @contextmanager
    def scope():
        sess = Session(engine)
        try:
            yield sess
            sess.commit()
        finally:
            sess.close()

    monkeypatch.setattr(app.db, "session_scope", scope)
    return monkeypatch


def test_match_all_selects_best_flags_on_server_and_reports_progress(env):
    def fake_search(query, limit=5):
        if "Archangel" in query:
            return [_song("Archangel", "Burial", "u-arch"),
                    _song("Archangel (Live)", "Someone", "u-live")]
        if "Windowlicker" in query:
            return [_song("Windowlicker", "Aphex Twin", "u-wl")]
        return []
    env.setattr(search, "search_songs", fake_search)

    seen = []
    lines = parse_lines("Burial - Archangel\nAphex Twin - Windowlicker\njunkline")
    matches = matching.match_all(1, lines, progress=lambda d, t: seen.append((d, t)))

    by_raw = {m.line.raw: m for m in matches}
    arch = by_raw["Burial - Archangel"]
    assert arch.best.url == "u-arch"           # correct candidate wins over the "(Live)" one
    assert arch.confidence == pytest.approx(1.0)
    assert arch.on_server is True              # already in library → pre-uncheck in the UI
    assert by_raw["Aphex Twin - Windowlicker"].best.url == "u-wl"
    assert by_raw["Aphex Twin - Windowlicker"].on_server is False
    assert by_raw["junkline"].best is None     # unparseable → empty match, not dropped
    assert seen[-1] == (3, 3)                  # progress reached total


def test_match_all_search_error_degrades_to_unmatched(env):
    def boom(query, limit=5):
        raise search.SearchError("down")
    env.setattr(search, "search_songs", boom)
    (m,) = matching.match_all(1, parse_lines("A - B"))
    assert m.best is None and m.candidates == []   # not fatal
