"""Shared visual theme (Vibrant / Glass, dark) and app shell."""
from __future__ import annotations

from contextlib import contextmanager

from nicegui import app, ui

from app.auth import current_display_name, load_user_language, set_user_language
from app.db import session_scope
from app.i18n import SUPPORTED_LANGUAGES, current_language, t

_HEAD_CSS = """
<style>
  body, .body--dark, .q-page-container {
    background:
      radial-gradient(1200px 800px at 18% -10%, #241b54 0%, transparent 58%),
      radial-gradient(1000px 700px at 100% 0%, #07303f 0%, transparent 52%),
      #0a0a14 !important;
    background-attachment: fixed !important;
  }
  .glass {
    background: rgba(255,255,255,0.055);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid rgba(255,255,255,0.10);
  }
  .accent-grad { background-image: linear-gradient(135deg,#7c3aed 0%,#06b6d4 100%); }
  .accent-text {
    background: linear-gradient(135deg,#a78bfa,#22d3ee);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .hover-glow:hover { box-shadow: 0 0 24px rgba(124,58,237,0.45); }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 8px; }
</style>
"""


def apply_base_style() -> None:
    ui.dark_mode(True)
    ui.colors(primary="#7c3aed", secondary="#06b6d4", accent="#22d3ee",
              dark="#0a0a14", dark_page="#0a0a14")
    ui.add_head_html(_HEAD_CSS)


def _nav_link(label: str, target: str, key: str, active: str) -> None:
    cls = "px-3 py-1.5 rounded-lg text-sm transition no-underline"
    if key == active:
        cls += " accent-grad text-white shadow"
    else:
        cls += " text-white/80 hover:text-white hover:bg-white/10"
    ui.link(label, target).classes(cls).style("text-decoration:none")


def _language_selector() -> None:
    """Top-bar language switcher; persists the choice and reloads the page."""
    def _on_change(e) -> None:
        set_user_language(e.value)
        ui.navigate.reload()  # re-render every string in the new language

    with ui.row().classes("items-center gap-1"):
        ui.icon("language").classes("text-white/60")
        ui.select(SUPPORTED_LANGUAGES, value=current_language(), on_change=_on_change) \
            .props("dense borderless dark options-dense").classes("text-sm text-white/80") \
            .tooltip(t("nav.language"))


def tag_option_switches(values) -> dict:
    """Render the six metadata-field switches (issue #7), prefilled from `values`
    (a `fix_music_tags.TagOptions`). Returns ``{field_name: ui.switch}``.

    Shared by the settings and download pages so the two can't drift; iterates
    `TAG_OPTION_FIELDS`, labelling each via the ``meta.<field>`` i18n key.
    """
    from app.fix_music_tags import TAG_OPTION_FIELDS  # lazy: keep mutagen off theme import

    switches: dict = {}
    ui.label(t("meta.desc")).classes("text-xs text-white/50")
    with ui.row().classes("w-full gap-x-8 gap-y-1 flex-wrap"):
        for f in TAG_OPTION_FIELDS:
            switches[f] = ui.switch(t(f"meta.{f}"), value=bool(getattr(values, f))) \
                .props("dense color=primary").classes("text-sm")
    return switches


@contextmanager
def frame(active: str = "download"):
    """Render the app shell and yield the page content container."""
    apply_base_style()
    # Hydrate the session language from the user's stored preference once.
    if "lang" not in app.storage.user:
        with session_scope() as session:
            app.storage.user["lang"] = load_user_language(session)

    with ui.element("div").classes("glass sticky top-0 z-50 w-full"):
        with ui.row().classes("w-full max-w-5xl mx-auto items-center justify-between px-6 py-3"):
            with ui.link(target="/").classes("flex items-center gap-2 no-underline"):
                ui.html('<img src="/static/soundpull-icon.svg" alt="Soundpull" class="h-7 w-7">')
                ui.html('<span class="text-lg font-bold tracking-tight text-white">'
                        'Sound<span style="color:#2CC2B3">pull</span></span>')
            with ui.row().classes("items-center gap-1"):
                _nav_link(t("nav.download"), "/", "download", active)
                _nav_link(t("nav.history"), "/history", "history", active)
                _nav_link(t("nav.settings"), "/settings", "settings", active)
                ui.element("div").classes("w-px h-6 bg-white/15 mx-2")
                _language_selector()
                ui.element("div").classes("w-px h-6 bg-white/15 mx-2")
                ui.label(current_display_name()).classes("text-sm text-white/70")
                ui.button(icon="logout", on_click=lambda: ui.navigate.to("/logout")) \
                    .props("flat round dense").classes("text-white/80").tooltip(t("nav.logout"))

    with ui.column().classes("w-full max-w-3xl mx-auto px-6 py-6 gap-4") as content:
        yield content
