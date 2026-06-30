from app.genres import ALLOWED_GENRES, DEFAULT_GENRE, normalize_genre


def test_known_genre_passes_through():
    assert normalize_genre("Pop") == "Pop"


def test_unknown_genre_falls_back():
    assert normalize_genre("not-a-genre") == DEFAULT_GENRE
    assert normalize_genre(None) == DEFAULT_GENRE


def test_default_genre_is_allowed():
    assert DEFAULT_GENRE in ALLOWED_GENRES
