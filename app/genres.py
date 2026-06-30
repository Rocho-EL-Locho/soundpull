"""Single source of truth for the selectable genres.

Lifted verbatim from the original `download_api.py` / `popup.html` so behaviour
matches the old system exactly.
"""
from __future__ import annotations

ALLOWED_GENRES: list[str] = [
    "Rap",
    "Hip-Hop",
    "R&B",
    "Pop",
    "Rock",
    "Electronic",
    "DnB",
    "Jazz",
    "Classical",
]

DEFAULT_GENRE = "Rap"


def normalize_genre(genre: str | None) -> str:
    """Return a valid genre, falling back to the default (mirrors download_api.py)."""
    if genre and genre in ALLOWED_GENRES:
        return genre
    return DEFAULT_GENRE
