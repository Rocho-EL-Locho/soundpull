"""Guards the Navidrome tag rules (feat-artist handling) — the crown jewel."""
from app.fix_music_tags import parse_featured_artists, split_artists


def test_split_artists_separators():
    assert split_artists("A & B, C") == ["A", "B", "C"]
    assert split_artists("A und B") == ["A", "B"]


def test_feat_in_title_moves_to_artist_and_cleans_title():
    title, artist, album_artist = parse_featured_artists("Song (feat. B)", "A, B")
    assert title == "Song"
    assert artist == "A / B"          # Primary / Featured
    assert album_artist == "A"        # album artist = primary only


def test_comma_list_without_feat_is_normalized():
    title, artist, album_artist = parse_featured_artists("Song", "A, B")
    assert artist == "A / B"
    assert album_artist == "A"


def test_plain_single_artist_unchanged():
    assert parse_featured_artists("Song", "A") == ("Song", "A", "A")
