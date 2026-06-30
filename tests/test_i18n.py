"""i18n catalog integrity + the `t()` resolver's fallback behavior.

These guard the things that silently rot as strings are added: a key present in
one language but not another, a renamed `{slot}` that would KeyError at runtime,
and audio-format labels drifting from app.pipeline.AUDIO_FORMATS.
"""
from string import Formatter

from app.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    TRANSLATIONS,
    audio_format_labels,
    t,
)
from app.pipeline import AUDIO_FORMATS


def _slots(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


def test_default_language_is_supported():
    assert DEFAULT_LANGUAGE in SUPPORTED_LANGUAGES
    assert set(SUPPORTED_LANGUAGES) == set(TRANSLATIONS)


def test_all_languages_share_the_same_keys():
    base = set(TRANSLATIONS[DEFAULT_LANGUAGE])
    for lang, table in TRANSLATIONS.items():
        assert set(table) == base, f"{lang} key set differs from {DEFAULT_LANGUAGE}"


def test_format_slots_match_across_languages():
    for key, base_text in TRANSLATIONS[DEFAULT_LANGUAGE].items():
        base_slots = _slots(base_text)
        for lang, table in TRANSLATIONS.items():
            assert _slots(table[key]) == base_slots, f"{lang}:{key} has mismatched {{slots}}"


def test_audio_labels_cover_every_format():
    # Outside a request context current_language() falls back to the default.
    assert set(audio_format_labels()) == set(AUDIO_FORMATS)


def test_t_falls_back_to_key_when_missing():
    assert t("does.not.exist") == "does.not.exist"


def test_t_tolerates_missing_format_slot():
    # A slotted template called without the slot must not raise — it degrades
    # to the raw template instead of crashing the page.
    assert t("index.track") == TRANSLATIONS[DEFAULT_LANGUAGE]["index.track"]


def test_t_uses_default_language_outside_request_context():
    assert t("nav.history") == TRANSLATIONS[DEFAULT_LANGUAGE]["nav.history"]
