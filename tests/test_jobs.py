"""Job worker helpers (issue #21)."""
from app.jobs import _clean_error


def test_clean_error_strips_ansi_colour_codes():
    # yt-dlp colourises errors; the stored/displayed message must be clean text.
    colored = "\x1b[0;31mERROR:\x1b[0m [youtube] kFl4bPPLlhg: Video unavailable"
    assert _clean_error(Exception(colored)) == "ERROR: [youtube] kFl4bPPLlhg: Video unavailable"


def test_clean_error_plain_text_unchanged():
    assert _clean_error(ValueError("kein WebDAV-Ziel")) == "kein WebDAV-Ziel"
