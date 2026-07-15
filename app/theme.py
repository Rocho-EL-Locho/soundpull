"""Shared visual theme (Vibrant / Glass, dark) and the sidebar app shell."""
from __future__ import annotations

from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version

from nicegui import app, context, ui

from app.auth import current_display_name, load_user_language, set_user_language
from app.db import session_scope
from app.i18n import SUPPORTED_LANGUAGES, current_language, t

try:
    _APP_VERSION = version("soundpull")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    _APP_VERSION = "0.0.0"

# Brand mark (the same paths as app/static/soundpull-icon.svg). currentColor is
# the brand teal set per-use; reused in the header (34px) and footer (18px).
_LOGO_SVG = (
    '<svg width="{size}" height="{size}" viewBox="0 0 120 120" fill="none" style="color:#2CC2B3">'
    '<rect x="22" y="42" width="10" height="22" rx="5" fill="currentColor"/>'
    '<rect x="38" y="26" width="10" height="38" rx="5" fill="currentColor"/>'
    '<rect x="54" y="16" width="10" height="48" rx="5" fill="currentColor"/>'
    '<rect x="70" y="32" width="10" height="32" rx="5" fill="currentColor"/>'
    '<rect x="86" y="44" width="10" height="20" rx="5" fill="currentColor"/>'
    '<polyline points="42,74 60,92 78,74" fill="none" stroke="currentColor" stroke-width="9" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<line x1="34" y1="104" x2="86" y2="104" stroke="currentColor" stroke-width="9" stroke-linecap="round"/>'
    '</svg>'
)

_WORDMARK = (
    '<span style="font-family:\'Space Grotesk\',sans-serif;font-size:24px;font-weight:700;'
    'letter-spacing:-0.5px">Sound<span style="color:#2CC2B3">pull</span></span>'
)

# Sidebar entries: (i18n key, route, active-key, Material icon).
_NAV_ITEMS = [
    ("nav.download", "/", "download", "download"),
    ("nav.library", "/library", "library", "library_music"),
    ("nav.duplicates", "/duplicates", "duplicates", "content_copy"),
    ("nav.health", "/health", "health", "health_and_safety"),
    ("nav.history", "/history", "history", "history"),
    ("nav.subscriptions", "/subscriptions", "subscriptions", "sync"),
    ("nav.settings", "/settings", "settings", "settings"),
]

# route path → active-key, for reactive sidebar highlighting under client-side routing.
_ROUTE_ACTIVE = {route: active_key for _, route, active_key, _ in _NAV_ITEMS}

# Sidebar width (px). An open drawer pushes the page content right by this much; the
# content wrapper compensates for it so the column stays centred under the header logo.
_DRAWER_WIDTH = 244


def _active_key_for_path(path: str) -> str:
    """Map a (possibly query/fragment-carrying) URL path to its sidebar active-key."""
    clean = path.split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"
    return _ROUTE_ACTIVE.get(clean, "download")

# External links in the footer. (i18n key or literal, Material icon, url).
_FOOTER_LINKS = [
    ("footer.github", "code", "https://github.com/Rocho-EL-Locho/soundpull"),
    ("footer.issues", "bug_report", "https://github.com/Rocho-EL-Locho/soundpull/issues"),
    ("footer.license", "gavel", "https://github.com/Rocho-EL-Locho/soundpull/blob/main/LICENSE"),
    ("yt-dlp", "open_in_new", "https://github.com/yt-dlp/yt-dlp"),
    ("Navidrome", "open_in_new", "https://www.navidrome.org/"),
]

_HEAD_CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  body, .q-field, .q-btn, .q-item, input, button {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
  }
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

  /* App shell surfaces */
  .sp-header {
    background: rgba(255,255,255,0.03) !important;
    backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    border-bottom: 1px solid rgba(255,255,255,0.08);
    color: #fff;
  }
  .sp-drawer {
    background: rgba(255,255,255,0.02) !important;
    border-right: 1px solid rgba(255,255,255,0.08);
  }
  .sp-footer {
    background: rgba(255,255,255,0.02) !important;
    backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    border-top: 1px solid rgba(255,255,255,0.08);
    color: rgba(255,255,255,0.4);
  }

  /* Hamburger */
  .sp-hamburger {
    display: flex; align-items: center; justify-content: center;
    width: 42px; height: 42px; border-radius: 12px;
    border: 1px solid rgba(255,255,255,0.12);
    background: rgba(255,255,255,0.05);
    color: rgba(255,255,255,0.85);
    cursor: pointer; transition: background .18s, box-shadow .18s;
  }
  .sp-hamburger:hover {
    background: rgba(255,255,255,0.12);
    box-shadow: 0 0 20px rgba(124,58,237,0.35);
  }

  /* Sidebar nav items */
  .sp-nav-item {
    display: flex; align-items: center; gap: 13px; width: 100%;
    padding: 11px 14px; border-radius: 12px;
    color: rgba(255,255,255,0.72); text-decoration: none;
    transition: background .16s, box-shadow .16s; cursor: pointer;
  }
  .sp-nav-item:hover { background: rgba(255,255,255,0.09); color: #fff; }
  .sp-nav-item .sp-nav-label {
    font-family: 'Space Grotesk', sans-serif; font-weight: 500; font-size: 15px;
  }
  .sp-nav-active, .sp-nav-active:hover {
    background: linear-gradient(135deg,#7c3aed,#06b6d4); color: #fff;
    box-shadow: 0 6px 20px rgba(124,58,237,0.35);
  }

  /* Footer links */
  .sp-footer-link {
    display: flex; align-items: center; gap: 7px;
    color: rgba(255,255,255,0.6); font-size: 13px; text-decoration: none;
    padding: 7px 12px; border-radius: 9px;
    transition: background .15s, color .15s;
  }
  .sp-footer-link:hover { background: rgba(255,255,255,0.09); color: #fff; }

  /* Destination selector cards (download page) */
  .sp-dest-card {
    display: flex; align-items: center; gap: 14px; flex: 1 1 0; min-width: 0;
    padding: 15px 18px; border-radius: 14px; cursor: pointer;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.10);
    transition: background .16s, border-color .16s, box-shadow .16s;
  }
  .sp-dest-card:hover {
    background: rgba(255,255,255,0.06);
    border-color: rgba(255,255,255,0.20);
  }
  .sp-dest-card-active, .sp-dest-card-active:hover {
    border-color: rgba(124,58,237,0.85);
    background: linear-gradient(135deg, rgba(124,58,237,0.22), rgba(6,182,212,0.12));
    box-shadow: 0 0 0 1px rgba(124,58,237,0.45), 0 10px 26px rgba(124,58,237,0.18);
  }
  .sp-dest-card .sp-dest-title {
    font-family: 'Space Grotesk', sans-serif; font-weight: 600; font-size: 15px; color: #fff;
  }

  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.15); border-radius: 8px; }
</style>
"""


def apply_base_style() -> None:
    ui.dark_mode(True)
    ui.colors(primary="#7c3aed", secondary="#06b6d4", accent="#22d3ee",
              dark="#0a0a14", dark_page="#0a0a14")
    ui.add_head_html(_HEAD_CSS)


def _language_selector() -> None:
    """Header language switcher; persists the choice and reloads the page."""
    def _on_change(e) -> None:
        set_user_language(e.value)
        ui.navigate.reload()  # re-render every string in the new language

    with ui.row().classes("items-center gap-1"):
        ui.icon("language").classes("text-white/60")
        ui.select(SUPPORTED_LANGUAGES, value=current_language(), on_change=_on_change) \
            .props("dense borderless dark options-dense").classes("text-sm text-white/80") \
            .tooltip(t("nav.language"))


def _sidebar_item(key: str, target: str, active_key: str, icon: str, active: str):
    cls = "sp-nav-item" + (" sp-nav-active" if active_key == active else "")
    with ui.link(target=target).classes(cls).style("text-decoration:none") as link:
        ui.icon(icon, size="22px")
        ui.label(t(key)).classes("sp-nav-label")
    return link


def _build_header(drawer) -> None:
    """Top bar: hamburger (toggles the sidebar) · centered logo · user zone."""
    def _toggle() -> None:
        drawer.toggle()
        app.storage.user["sidebar_open"] = bool(drawer.value)

    with ui.header(elevated=False).classes("sp-header").style(
        "padding:14px 22px; align-items:center; justify-content:space-between; gap:14px"
    ):
        # Left: hamburger
        with ui.element("div").style("flex:1; display:flex; align-items:center"):
            ham = ui.element("button").classes("sp-hamburger") \
                .on("click", _toggle).tooltip(t("nav.menu"))
            with ham:
                icon = ui.icon("menu_open", size="24px")
                icon.bind_name_from(drawer, "value",
                                    backward=lambda v: "menu_open" if v else "menu")

        # Center: logo lockup (links home)
        with ui.link(target="/").classes("no-underline").style(
            "flex:0 0 auto; display:flex; align-items:center; gap:11px; text-decoration:none"
        ):
            ui.html(_LOGO_SVG.format(size=34))
            ui.html(_WORDMARK)

        # Right: language · divider · user · logout
        with ui.element("div").style(
            "flex:1; display:flex; align-items:center; justify-content:flex-end; gap:14px"
        ):
            _language_selector()
            ui.element("div").style("width:1px; height:22px; background:rgba(255,255,255,0.14)")
            name = current_display_name()
            with ui.row().classes("items-center gap-2"):
                with ui.element("div").style(
                    "width:30px; height:30px; border-radius:50%; display:flex; align-items:center;"
                    "justify-content:center; font-size:13px; font-weight:600; color:#fff;"
                    "background:linear-gradient(135deg,#7c3aed,#06b6d4)"
                ):
                    ui.label((name or "?")[:1].upper())
                ui.label(name).classes("text-sm text-white/75")
            ui.button(icon="logout", on_click=lambda: ui.navigate.to("/logout")) \
                .props("flat round dense").classes("text-white/60").tooltip(t("nav.logout"))


def _build_sidebar(active: str):
    """Collapsible left sidebar. Returns ``(drawer, nav_items)`` where ``nav_items`` is a
    list of ``(link_element, active_key)`` so the shell can re-highlight the active entry
    on client-side (``ui.sub_pages``) navigation without rebuilding the drawer."""
    sidebar_open = bool(app.storage.user.get("sidebar_open", True))
    drawer = ui.left_drawer(value=sidebar_open, fixed=True, bordered=False, elevated=False) \
        .classes("sp-drawer").props(f"width={_DRAWER_WIDTH}").style("padding:0")

    def _close() -> None:
        drawer.hide()
        app.storage.user["sidebar_open"] = False

    nav_items: list = []
    with drawer:
        with ui.column().classes("w-full").style("height:100%; padding:20px 14px; gap:6px"):
            # Explicit close button: on desktop the open drawer covers the header's
            # hamburger toggle, and there is no click-outside backdrop in push mode — so
            # this is the only always-reachable way to collapse the sidebar without a
            # touch swipe.
            with ui.row().classes("w-full").style("justify-content:flex-end; margin-bottom:2px"):
                ui.button(icon="close", on_click=_close).props("flat round dense") \
                    .classes("text-white/60").tooltip(t("nav.close_menu"))
            for key, target, active_key, icon in _NAV_ITEMS:
                link = _sidebar_item(key, target, active_key, icon, active)
                nav_items.append((link, active_key))
            with ui.row().style(
                "margin-top:auto; padding-top:16px; border-top:1px solid rgba(255,255,255,0.08);"
                "align-items:center; gap:8px; color:rgba(255,255,255,0.35); font-size:12px"
            ):
                ui.icon("bolt", size="16px")
                ui.label(f"v{_APP_VERSION} · {t('nav.selfhosted')}")
    return drawer, nav_items


def _build_footer() -> None:
    with ui.footer(elevated=False).classes("sp-footer").style(
        "padding:18px 26px; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:14px"
    ):
        with ui.row().classes("items-center gap-2").style("color:rgba(255,255,255,0.4); font-size:13px"):
            ui.html(_LOGO_SVG.format(size=18))
            ui.label(t("footer.tagline"))
        with ui.row().classes("items-center gap-1 flex-wrap"):
            for key, icon, url in _FOOTER_LINKS:
                label = t(key) if "." in key else key
                with ui.link(target=url, new_tab=True).classes("sp-footer-link"):
                    ui.icon(icon, size="18px")
                    ui.label(label)


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
def frame():
    """Render the persistent sidebar app shell **once** and yield the content container
    that hosts the client-side ``ui.sub_pages`` router.

    The shell (drawer/header/footer) is built a single time per full page load; page
    switches only swap the sub-page content, so there is no full reload. The active
    sidebar entry is re-highlighted reactively via the sub-pages router's path-change
    callback (see `_active_key_for_path`)."""
    apply_base_style()
    # Hydrate the session language from the user's stored preference once.
    if "lang" not in app.storage.user:
        with session_scope() as session:
            app.storage.user["lang"] = load_user_language(session)

    # The shell owns all spacing; drop NiceGUI's default page-content padding/gap.
    context.client.content.classes("!p-0 !gap-0")

    router = context.client.sub_pages_router
    active = _active_key_for_path(router.current_path)

    # Layout elements (header/drawer/footer) must be created as direct children of
    # the page content — auto-placed by NiceGUI into the Quasar layout regardless
    # of creation order. Build the drawer first so the header can toggle it.
    drawer, nav_items = _build_sidebar(active)
    _build_header(drawer)
    _build_footer()

    # Re-highlight the active entry when the client-side route changes (no reload).
    def _sync_active(path: str) -> None:
        key = _active_key_for_path(path)
        for link, active_key in nav_items:
            (link.classes(add="sp-nav-active") if active_key == key
             else link.classes(remove="sp-nav-active"))
    router.on_path_changed(_sync_active)

    # Centre the content under the header logo (the viewport centre). An open left drawer
    # pushes this container right by its width, while the full-width header keeps the logo
    # at the viewport centre — so add matching right padding to re-centre the column, and
    # drop it when the drawer is collapsed. Tracks the drawer's open/close live.
    content_wrap = ui.element("div").classes("w-full")

    def _center_under_logo(is_open: bool) -> None:
        right = 34 + (_DRAWER_WIDTH if is_open else 0)
        content_wrap.style(f"padding:30px {right}px 48px 34px")

    _center_under_logo(bool(drawer.value))
    drawer.on_value_change(lambda e: _center_under_logo(bool(e.value)))

    with content_wrap:
        with ui.column().classes("w-full items-stretch") \
                .style("max-width:760px; margin:0 auto; gap:16px") as content:
            yield content
