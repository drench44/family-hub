"""Frontend contract tests — no browser, no server.

The wall page has a JS runner for its pure helpers (test_js.py); these pin the
file-level conventions the design spec is built on: every referenced class is
styled, the house-climate palette tokens are present, scripts load in order,
and — because the three full-screen iframe URLs come from the API at runtime —
no literal http(s) URL appears anywhere in the committed frontend (a LAN wall
display reaches nothing on the internet).
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "family_hub" / "web" / "static"

CSS = (STATIC / "styles.css").read_text()
HTML_FILES = sorted(STATIC.glob("*.html"))
JS_FILES = sorted(STATIC.glob("*.js"))
ALL_HTML = "\n".join(p.read_text() for p in HTML_FILES)
ALL_JS = "\n".join(p.read_text() for p in JS_FILES)

# The NEW load-bearing theme tokens (Task 10 finished the migration off the
# legacy dark-only names). Every one must be defined so the whole wall chrome
# resolves in BOTH themes.
PALETTE_TOKENS = ["--ground", "--surface", "--surface-2", "--edge", "--edge-soft",
                  "--ink", "--dim", "--faint", "--good", "--warn", "--crit",
                  "--accent", "--accent-ink", "--accent-soft"]

_CLASS_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*$")
# `f-*` are query hooks for common.js's reusable chore form — they exist to be
# selected in JS, never to be styled (the visible styling rides sibling classes
# like .segmented / .day-chips / .txt-input). Listed here on purpose.
UNSTYLED_OK = {"f-title", "f-icon", "f-repeat", "f-days", "f-assign",
               "f-person", "f-rotadd", "f-rotation", "f-rothint", "f-error"}


def _css_rule(selector):
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        if re.search(rf"{re.escape(selector)}(?![\w-])", m.group(1)):
            out.append(m.group(2))
    return "\n".join(out)


def _referenced_classes():
    refs = set()
    for m in re.finditer(r'class="([^"]*)"', ALL_HTML + ALL_JS):
        for tok in m.group(1).split():
            if "${" not in tok and _CLASS_TOKEN.fullmatch(tok):
                refs.add(tok)
    for m in re.finditer(r"classList\.(?:add|toggle|remove)\('([\w-]+)'", ALL_JS):
        refs.add(m.group(1))
    # conditional class appends inside template expressions: `${c ? ' foo' : ''}`
    for m in re.finditer(r"\?\s*'\s+([A-Za-z][A-Za-z0-9_ -]*)'", ALL_JS):
        for tok in m.group(1).split():
            if _CLASS_TOKEN.fullmatch(tok):
                refs.add(tok)
    return refs


def test_every_referenced_class_is_styled():
    missing = sorted(
        c for c in _referenced_classes()
        if c not in UNSTYLED_OK and not re.search(rf"\.{re.escape(c)}(?![\w-])", CSS)
    )
    assert missing == [], f"classes referenced but absent from styles.css: {missing}"


def test_palette_tokens_present():
    for tok in PALETTE_TOKENS:
        assert f"{tok}:" in CSS, f"missing palette token {tok}"


@pytest.mark.parametrize("html_path", HTML_FILES, ids=lambda p: p.name)
def test_common_js_loads_before_page_script(html_path):
    # strip cache-buster queries (?v=N) before comparing names
    scripts = [s.split("?")[0] for s in
               re.findall(r'<script src="([^"]+)"', html_path.read_text())]
    page = [s for s in scripts if s in ("hub.js",)]
    if page:
        assert "common.js" in scripts
        for p in page:
            assert scripts.index("common.js") < scripts.index(p), \
                f"{p} depends on common.js globals — order matters"


@pytest.mark.parametrize("html_path", HTML_FILES, ids=lambda p: p.name)
def test_viewport_meta_present(html_path):
    assert 'name="viewport"' in html_path.read_text(), \
        f"{html_path.name} missing the viewport meta"


def test_no_external_resources():
    for p in HTML_FILES + JS_FILES:
        assert "@import" not in p.read_text(), f"{p.name} pulls a remote stylesheet"
    assert "@import" not in CSS


def test_no_literal_http_urls_anywhere():
    """The three overlay iframe URLs are delivered by /api/hub at runtime, so a
    committed http(s) literal would either be dead or a smuggled external ref."""
    for label, text in (("html", ALL_HTML), ("js", ALL_JS), ("css", CSS)):
        found = re.findall(r"https?://\S+", text)
        assert not found, f"literal URL(s) in {label}: {found}"


# ------------------------------------------------- design-spec additions

def test_chore_row_is_a_big_tap_target():
    # was >= 64; compacted to fit more chores per column (operator,
    # 2026-08-15). 48px stays the floor — the common touch-target minimum.
    rule = _css_rule(".chore-row")
    m = re.search(r"min-height:\s*(\d+)px", rule)
    assert m and int(m.group(1)) >= 48, ".chore-row must be >= 48px (tap target)"


def test_reminder_list_picker_avoids_ios_zoom_and_is_a_tap_target():
    # The reminders add row's list <select> shows on the phone To-Dos tab. A
    # sub-16px font makes iOS Safari zoom the page on focus (the same trap the
    # .todo-add input avoids), and it needs a real tap target. Pin both.
    rule = _css_rule(".todo-list-select")
    assert rule.strip(), ".todo-list-select must be styled"
    fs = re.search(r"font-size:\s*(\d+)px", rule)
    assert fs and int(fs.group(1)) >= 16, "list picker font must be >= 16px (no iOS zoom-on-focus)"
    mh = re.search(r"min-height:\s*(\d+)px", rule)
    assert mh and int(mh.group(1)) >= 44, "list picker must be a >= 44px tap target"


def test_todo_and_reminder_rows_and_actions_are_big_tap_targets():
    # The to-do / reminder rows and the delete/move action pills are all real
    # touch targets on the wall and the phone. Only .chore-row was guarded; pin
    # these too so a future compaction can't quietly shrink them below the
    # ~44px floor (CLAUDE.md). Rows share one rule; the action pills another.
    row = _css_rule(".todo-row, .todo-row-full")
    rm = re.search(r"min-height:\s*(\d+)px", row)
    assert rm and int(rm.group(1)) >= 44, ".todo-row(-full) must be a >= 44px tap target"
    act = _css_rule(".todo-act")
    am = re.search(r"min-height:\s*(\d+)px", act)
    assert am and int(am.group(1)) >= 44, ".todo-act (delete/move) must be a >= 44px tap target"


def test_reminder_bucket_list_reuses_the_shared_row_and_box_classes():
    # The iCloud reminders view is built from the same .todo-row(-full)/.card
    # chrome as the local list — not a parallel styling system that could drift.
    # Pin that the source-chip + stacked-list classes it DOES add are styled.
    for cls in (".shead-chip", ".rem-list", ".rem-due", ".rem-pri"):
        assert _css_rule(cls).strip(), f"{cls} (iCloud reminders view) is unstyled"


def test_celebration_is_reduced_motion_guarded():
    assert ".card-celebrate" in CSS, "missing the completion celebration class"
    assert "prefers-reduced-motion" in CSS, "celebration must be reduced-motion guarded"


def test_week_strip_state_classes_are_styled():
    for cls in (".ws-done", ".ws-partial", ".ws-none", ".ws-rest"):
        assert _css_rule(cls).strip(), f"{cls} week-strip state is unstyled"


def test_overlay_and_home_pill_styled():
    for cls in (".overlay", ".overlay-home"):
        assert _css_rule(cls).strip(), f"{cls} is unstyled"


def test_overlay_home_pill_stays_tappable_over_scaled_iframes():
    """The ⌂ home pill sits over the overlay content, which for a "fit" panel
    (weather/climate) is an iframe scaled with a CSS transform. On iOS Safari
    taps over that scaled iframe fall THROUGH the pill unless it owns its own
    compositing layer — leaving no way to exit the overlay (operator report,
    2026-08-15). The fix is a non-obvious one-liner that reads like removable
    cruft, so pin it: the pill must keep its own layer and sit clearly on top."""
    rule = _css_rule(".overlay-home")
    assert "translateZ(0)" in rule or "translate3d" in rule, \
        "overlay-home needs its own compositing layer or taps fall through the " \
        "scaled fit-panel iframe on iOS"
    z = re.search(r"z-index:\s*(\d+)", rule)
    assert z and int(z.group(1)) >= 10, \
        "overlay-home must sit above the overlay panel content"


def test_mobile_tabbar_stays_tappable():
    """The phone tab bar must be an IN-FLOW row at the bottom of the body flex
    column (the app shell), NOT a fixed bar floating over the scrolling page.
    A fixed bar kept going untappable on iOS Safari — a `backdrop-filter` blur
    made taps fall through it, and a `transform` made its hit area misalign when
    scrolled to the bottom (operator reports, 2026-08-15). An in-flow, solid,
    transform-free bar is reliably tappable at every scroll position. Pin all of
    that so a regression to the fixed-bar approach can't silently return."""
    # strip comments (so the rule's prose can't trip the checks) then whitespace
    # (so `position:fixed` / `position: fixed` and any reformatting both match)
    ns = re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", _css_rule(".tabbar"), flags=re.S))
    assert "backdrop-filter:" not in ns, \
        "the tab bar must not use backdrop-filter — it breaks taps on iOS Safari"
    assert "transparent" not in ns, \
        "the tab bar background must be solid, not translucent"
    assert "position:fixed" not in ns, \
        "the tab bar must be in-flow (app shell), not fixed over the scroll area"
    assert "translateZ" not in ns and "translate3d" not in ns, \
        "no transform on the bar — it misaligns a fixed bar's taps on iOS"
    assert "position:static" in ns, \
        "the phone tab bar must be pinned in-flow (position: static)"


# The phone-shell rules are bounded by these marker comments in styles.css. They
# are a width media query (so the phone layout works with NO JS), and every rule
# is guarded by :root:not([data-layout="desktop"]) so choosing "desktop" in
# Settings suppresses the whole shell at any width (the Fire-TV escape hatch).
PHONE_SHELL_START = "/* >>> phone shell"
PHONE_SHELL_END = "/* <<< phone shell"
# The guard prefix every phone-shell rule carries.
SHELL_GUARD = ':root:not([data-layout="desktop"])'


def _phone_shell_css():
    """The phone-shell block, comments stripped. Bounded by the marker comments
    rather than a media query, since the shell is now attribute-keyed."""
    start = CSS.index(PHONE_SHELL_START)
    end = CSS.index(PHONE_SHELL_END)
    return re.sub(r"/\*.*?\*/", "", CSS[start:end], flags=re.S)


def _ns(s):
    """Whitespace-proof: strip spaces so `overflow:hidden` / `overflow: hidden`
    (and any future reformatting) both match."""
    return re.sub(r"\s+", "", s)


def _shell_rule(selector):
    """The declaration block of the phone-shell rule whose selector is EXACTLY
    `selector` (whitespace-insensitive), or None. Exact match avoids picking the
    combined `html, body` fallback rule when asked for the single `body` rule."""
    target = _ns(selector)
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", _phone_shell_css()):
        if _ns(m.group(1)) == target:
            return _ns(m.group(2))
    return None


def test_mobile_app_shell_scrolls_content_not_the_body():
    """The phone layout is an app shell: the body is a fixed-height flex column
    that itself does NOT scroll, the content region (.wrap) scrolls inside it,
    and the in-flow tab bar is the last row. This is what keeps the tab bar
    tappable at any scroll position (it never overlaps content) and makes
    scroll-to-top a single-container reset. Guard the shape so it can't regress
    to a fixed-bar-over-scrolling-body layout, which failed on iOS. The shell is
    a width media query guarded by :root:not([data-layout="desktop"]) so it
    needs no JS and a forced-desktop choice suppresses it."""
    body = _shell_rule(f'{SHELL_GUARD} body')
    assert body, f'no phone shell body rule keyed on {SHELL_GUARD} body'
    assert "overflow:hidden" in body, \
        "phone body must not scroll — the .wrap content region does"
    assert "display:flex" in body and "flex-direction:column" in body, \
        "phone body must be a flex column app shell"
    # the body must size to the DYNAMIC viewport, driven by an innerHeight-backed
    # CSS var (--app-h) with a 100dvh fallback — see the tab-bar-gap fix. On iOS
    # a stale 100dvh after a bfcache restore left the in-flow tab bar floating
    # above a gap until a reload; the var is refreshed on pageshow/visibility.
    # It must ALSO clear the base rule's min-height:100vh floor (100vh > 100dvh
    # on iOS pushes the bar below the fold).
    assert "100dvh" in body, "phone body must fall back to the dynamic viewport (100dvh)"
    assert "var(--app-h" in body, \
        "phone body height must use the innerHeight-backed --app-h var (gap fix)"
    assert "min-height:0" in body or "min-height:100dvh" in body, \
        "phone body must clear the base min-height:100vh floor (min-height:0), " \
        "or the tab bar drops below the fold on iOS"
    wrap = _shell_rule(f'{SHELL_GUARD} .wrap')
    assert wrap, f'no phone .wrap rule keyed on {SHELL_GUARD} .wrap'
    assert "overflow-y:auto" in wrap, ".wrap must be the scrolling content region"
    # flex:1 + min-height:0 are load-bearing: without min-height:0 a flex item
    # won't shrink below its content, so overflow-y:auto is inert and .wrap
    # overflows the fixed-height body, shoving the tab bar off-screen
    assert "flex:1" in wrap and "min-height:0" in wrap, \
        ".wrap needs flex:1 + min-height:0 to actually scroll inside the shell"


def test_no_backdrop_filter_on_fixed_elements():
    """Generalizes the tab-bar fix into a rule for the whole stylesheet: a
    `backdrop-filter` on ANY position:fixed/sticky element goes intermittently
    untappable on iOS Safari (taps fall through it). We hit this on the tab bar
    (2026-08-15); this guard stops it recurring on any future fixed toolbar,
    banner, or bar. If a translucent-blur effect is truly wanted, put the blur
    on a non-interactive ::before/::after layer, not the interactive element."""
    offenders = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        # drop comments (so prose mentioning the property can't false-trip it)
        # then strip whitespace so `position:fixed` / `backdrop-filter :` can't
        # slip past the substring checks
        body = re.sub(r"/\*.*?\*/", "", m.group(2), flags=re.S)
        ns = re.sub(r"\s+", "", body)
        pinned = ("position:fixed" in ns or "position:sticky" in ns
                  or "position:-webkit-sticky" in ns)
        if pinned and "backdrop-filter:" in ns:
            offenders.append(m.group(1).strip().splitlines()[-1].strip())
    assert offenders == [], \
        f"backdrop-filter on a fixed/sticky element breaks taps on iOS: {offenders}"


def test_one_card_and_section_header_system_styled():
    """Task 2's headline global constraint: a single .card box treatment and a
    single .shead section header (with its tick + expand button) are the source
    of truth every section uses. Pin that they're all styled."""
    for cls in (".card", ".shead", ".tick", ".expand"):
        assert _css_rule(cls).strip(), f"{cls} is unstyled"
    # the one card carries the shared surface/border/radius/shadow
    card = _css_rule(".card")
    assert "var(--surface)" in card and "var(--edge)" in card, \
        ".card must use the shared surface/edge tokens"
    assert "border-radius" in card and "var(--shadow)" in card, \
        ".card must carry the one radius + the soft shadow"
    # the old ad-hoc section-header classes were removed, not left as dead CSS
    for dead in (".panel-head", ".panel-expand", ".sec-head"):
        assert not re.search(rf"{re.escape(dead)}(?![\w-])", CSS), \
            f"{dead} should be gone — every section uses .shead now"


SWATCH_HEXES = ["#FA4352", "#F64E06", "#BE7A05", "#978B04",
                "#5B9904", "#049F1E", "#049C6A", "#049E8C",
                "#0594C3", "#3587FA", "#717CFB", "#9371FB",
                "#B95DFB", "#E721F9", "#F928B4", "#FA3C7B"]

# The two card surfaces a person's name/border/checks are drawn on (Task 1):
# light theme's card is white, dark theme's card is this near-black navy.
LIGHT_SURFACE = "#FFFFFF"
DARK_SURFACE = "#141A26"
MIN_CONTRAST = 3.0   # WCAG AA-large bar; the person name is 18px bold


def _srgb_to_linear(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hexcolor):
    hexcolor = hexcolor.lstrip("#")
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (0, 2, 4))
    R, G, B = _srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)
    return 0.2126 * R + 0.7152 * G + 0.0722 * B


def _contrast_ratio(hex1, hex2):
    l1, l2 = _relative_luminance(hex1), _relative_luminance(hex2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def test_month_grid_and_event_card_styled():
    for cls in (".mgrid", ".mg-day", ".mg-today", ".mg-ev",
                ".ev-modal", ".ev-card", ".ev-close", ".cal-nav"):
        assert _css_rule(cls).strip(), f"{cls} is unstyled"


def test_climate_weather_out_of_range_visuals_are_styled():
    """The out-of-range coloring layer (House Climate + Weather cards): status
    tints, the UV/AQI ring gauges, the room thermometer + tile wash. These are
    built with dynamic `st-${band}` classes the generic referenced-class scan
    can't see, so guard them explicitly here so the visual can't silently
    regress."""
    # status text tints map to the CORRECT palette token (catches a color swap,
    # e.g. .st-warn accidentally using --crit)
    for band, token in (("good", "--good"), ("warn", "--warn"), ("crit", "--crit")):
        assert token in _css_rule(f".st-{band}"), f".st-{band} must tint with var({token})"
    # the UV/AQI/humidity ring gauge: a track plus a fill stroked per band
    assert _css_rule(".g-track").strip(), "the gauge track is unstyled"
    assert "var(--good)" in _css_rule(".g-fill.st-good")
    assert "var(--warn)" in _css_rule(".g-fill.st-warn")
    assert "var(--crit)" in _css_rule(".g-fill.st-crit")
    # the ring's centered value tints with its band too
    assert "var(--crit)" in _css_rule(".g-num.st-crit")
    # the room thermometer mercury carries each band color
    assert "var(--good)" in _css_rule(".t-merc.st-good")
    assert "var(--crit)" in _css_rule(".t-merc.st-crit")
    # an out-of-range room tile carries a wash keyed to its band
    assert "--warn" in _css_rule(".room.warn")
    assert "--crit" in _css_rule(".room.crit")
    # an out-of-range room cell out-ranks the base cell color (specificity guard:
    # `.room .rh` would otherwise beat a bare `.st-crit`)
    assert "var(--crit)" in _css_rule(".room .rh.st-crit")


def test_sky_scene_phase_and_condition_classes_are_styled():
    """The weather card's sky scene classes are built dynamically in hub.js
    (`sky ph-${phase} cn-${cond}`), so the generic referenced-class scan can't
    see them — deleting .sky.ph-night from styles.css would pass every test
    while the card renders an unstyled div all night. Guard each phase
    gradient, each condition's veil, and every scene layer explicitly."""
    for ph in ("day", "dawn", "dusk", "night"):
        assert _css_rule(f".sky.ph-{ph}").strip(), f".sky.ph-{ph} gradient is unstyled"
    for cn in ("cloudy", "rain", "storm", "snow", "fog"):
        assert _css_rule(f".sky.cn-{cn}").strip(), f".sky.cn-{cn} veil is unstyled"
    for cls in (".sky-sun", ".sky-moon", ".sky-stars", ".sky-cloud",
                ".sky-rain", ".sky-snow", ".sky-fog", ".sky-txt"):
        assert _css_rule(cls).strip(), f"{cls} scene layer is unstyled"
    # the moon-phase construction: moonHtml emits the direction classes + the
    # inline --m-term, so only these rules make the phase visible. A bare
    # .sky-moon (no classes) must draw neither part — that IS the full-disc
    # fallback for feed data that's missing or not understood.
    assert _css_rule(".sky-moon.m-waxing::after").strip(), \
        "the waxing shadow half-disc is unstyled"
    assert _css_rule(".sky-moon.m-waning::after").strip(), \
        "the waning shadow half-disc is unstyled"
    assert "var(--m-term" in _css_rule(".sky-moon.m-waxing::before"), \
        "the terminator ellipse must size from var(--m-term)"
    assert _css_rule(".sky-moon.m-gibbous::before").strip(), \
        "the gibbous (moon-colored) terminator is unstyled"
    # every ambient sky/chart layer is pinned off under prefers-reduced-motion
    # (the animation now lives on pseudo-elements for rain/snow — compositing)
    for cls in (".sky-sun", ".sky-stars", ".sky-cloud", ".sky-fog",
                ".sky-rain::before", ".sky-rain::after",
                ".sky-snow", ".sky-snow::before", ".sky-snow::after",
                ".spark .sp-halo"):
        assert "animation: none" in _css_rule(cls), \
            f"{cls} is not pinned off under prefers-reduced-motion"


def test_favicon_is_local_svg():
    for p in HTML_FILES:
        assert 'rel="icon" href="favicon.svg"' in p.read_text(), \
            f"{p.name} lost the local favicon"
    assert (STATIC / "favicon.svg").exists()


def test_admin_html_is_retired():
    """admin.html/admin.js were retired 2026-08-15 — all management now lives on
    the wall's Chores page (tap Edit). Guard against either file creeping back:
    the wall must stay the single admin surface, and no page may reference the
    dead admin route."""
    assert not (STATIC / "admin.html").exists(), "admin.html should be deleted"
    assert not (STATIC / "admin.js").exists(), "admin.js should be deleted"
    assert "admin.html" not in ALL_HTML + ALL_JS, \
        "no frontend file may still link to the retired /admin.html"


MOBILE_TABS = ("chores", "cal", "cams", "weather", "laundry")

# the wall sections that each live in the hub grid; a tab shows one and hides
# the rest. (.camgrid is not here — it is default-hidden and only the cams tab
# reveals it.)
WALL_SURFACES = {"people-col", "cal", "tiles", "panels"}

# each tab's surface, by the section class it leaves visible. The cams tab shows
# the .camgrid (a single stacked column on the phone) and suppresses the wall's
# .tiles column entirely, so its surface is none of the shared wall sections.
TAB_SURFACE = {"chores": ".people-col", "cal": ".cal",
               "cams": ".camgrid", "weather": ".panels",
               # laundry reuses .panels, filtered to the laundry slot by its
               # own child rule (asserted in test_laundry_card_static_guards)
               "laundry": ".panels"}


def test_mobile_reflow_block_present():
    """At phone width the wall page reflows to a phone layout with bottom tabs
    (operator request 2026-08-13); the fixed 1920 canvas stays above it. The
    reflow is a pure-CSS width media query (max-width:1000px) so it needs no JS,
    with every rule carrying the :root:not([data-layout="desktop"]) guard so
    choosing Desktop suppresses it at any width. Both markers must bound the
    block."""
    assert PHONE_SHELL_START in CSS and PHONE_SHELL_END in CSS, \
        "phone-shell marker comments missing"
    mobile = _phone_shell_css()
    # a width media query is the base (works with NO JS), guarded so forced
    # desktop suppresses it
    assert "@media (max-width: 1000px)" in mobile, \
        "phone shell must remain a width media query so it works without JS"
    assert SHELL_GUARD in mobile, \
        "phone-shell rules must carry the :root:not([data-layout=\"desktop\"]) guard"
    for tab in MOBILE_TABS:
        assert f'body[data-tab="{tab}"]' in mobile, \
            f"missing mobile visibility rules for the {tab} tab"


def test_each_tab_hides_every_other_surface():
    """Four tabs, one surface each (operator request 2026-08-13): a tab's
    rule must hide the other three sections, and never its own."""
    mobile = _phone_shell_css()
    for tab, own in TAB_SURFACE.items():
        block = re.search(
            rf'body\[data-tab="{tab}"\][^{{}}]*\{{[^}}]*\}}', mobile)
        assert block, f"no visibility rule for the {tab} tab"
        sel = mobile[mobile.index(f'body[data-tab="{tab}"]'):]
        sel = sel[:sel.index("}")]
        hidden = set(re.findall(r"\.(people-col|cal|tiles|panels)(?![\w-])", sel))
        # A tab hides every wall surface except the one it reuses. The cams tab
        # reuses none of them (it shows the .camgrid grid instead), so it hides
        # all four wall sections.
        expect = WALL_SURFACES - {own.lstrip(".")}
        assert hidden == expect, f"{tab} tab hides {hidden}, expected {expect}"
    # The cams tab must actually reveal the camera grid it suppressed the column
    # for, and stack it one-per-row on the phone (the 2x2 was unreadably small).
    cams_grid = re.search(r'body\[data-tab="cams"\] \.camgrid\s*\{([^}]*)\}', mobile)
    assert cams_grid, "cams tab must reveal .camgrid"
    assert "grid-template-columns: 1fr" in cams_grid.group(1), \
        "cams tab must stack cameras in a single column on the phone"


def test_layout_mode_control_present_and_wired():
    """The Auto/Desktop layout control lives in the display popover, is wired to
    setLayout in hub.js, and setLayout/stampLayout + the data-layout stamp exist
    in theme.js. The click wiring uses a descendant-combinator selector the
    fake-DOM harness can't exercise (gap 1 in hub-dom.test.mjs), so this static
    guard covers it; the reflection branch is DOM-tested there. Desktop is the
    escape hatch for a TV that mis-reports a phone width, so it must not silently
    disappear."""
    index = (STATIC / "index.html").read_text()
    theme = (STATIC / "theme.js").read_text()
    hub = (STATIC / "hub.js").read_text()
    for v in ("auto", "desktop"):
        assert f'data-layout-set="{v}"' in index, \
            f"display popover missing the {v} layout button"
    # "mobile" was removed as a layout value (Auto/Desktop only) — no dead button
    assert 'data-layout-set="mobile"' not in index, \
        "the mobile layout button was intentionally dropped (Auto/Desktop only)"
    # theme.js: persisted setter + stamp-only applier + the attribute stamp
    assert "window.setLayout" in theme, "theme.js must expose setLayout"
    assert "window.stampLayout" in theme, "theme.js must expose stampLayout"
    assert 'setAttribute("data-layout"' in theme, \
        "theme.js must stamp the data-layout choice attribute"
    # hub.js: a [data-layout-set] tap forwards to setLayout, and the control is
    # reflected (reflectThemeControls reads the data-layout-set buttons)
    assert re.search(r"data-layout-set\]'\)[\s\S]{0,80}setLayout\(", hub), \
        "hub.js must wire a [data-layout-set] tap to setLayout()"
    assert "data-layout-set" in hub, \
        "reflectThemeControls must reflect the [data-layout-set] buttons"


def test_idle_return_control_present_and_wired():
    """The Auto-return On/Off control lives in the display popover + full Settings,
    is wired to setIdleReturn in hub.js, and setIdleReturn/stampIdleReturn + the
    data-idle-return stamp exist in theme.js. The click wiring uses a
    descendant-combinator selector the fake-DOM harness can't exercise, so this
    static guard covers it; the reflection + armIdle branches are DOM-tested. This
    is the escape hatch so a personal phone/TV isn't yanked back to the home wall
    while someone is reading a full-screen view, so it must not silently vanish."""
    index = (STATIC / "index.html").read_text()
    theme = (STATIC / "theme.js").read_text()
    hub = (STATIC / "hub.js").read_text()
    for v in ("on", "off"):
        assert f'data-idle-set="{v}"' in index, \
            f"display popover missing the {v} auto-return button"
    # theme.js: persisted setter + stamp-only applier + the attribute stamp
    assert "window.setIdleReturn" in theme, "theme.js must expose setIdleReturn"
    assert "window.stampIdleReturn" in theme, "theme.js must expose stampIdleReturn"
    assert 'setAttribute("data-idle-return"' in theme, \
        "theme.js must stamp the data-idle-return choice attribute"
    # hub.js: a [data-idle-set] tap forwards to setIdleReturn, the control is
    # reflected, and armIdle honors the choice (skips arming when opted out)
    assert re.search(r"data-idle-set\]'\)[\s\S]{0,120}setIdleReturn\(", hub), \
        "hub.js must wire a [data-idle-set] tap to setIdleReturn()"
    assert "data-idle-set" in hub, \
        "reflectThemeControls must reflect the [data-idle-set] buttons"
    assert 'data-idle-return' in hub, \
        "armIdle must read the data-idle-return choice"


def test_camera_page_shows_four_per_screen_and_scrolls():
    # The full-screen camera page is two columns with a minmax row height:
    #   grid-auto-rows: minmax(calc((100% - <N>px) / 2), 1fr)
    # The 1fr ceiling lets four-or-fewer cameras fill the screen (no half-black
    # void); the exact-half calc floor makes >4 collapse so precisely four
    # cameras fill one screen (2x2, no peek — operator preference) and the rest
    # scroll. overflow-y makes them reachable. Guards the `.camera-page` block
    # only (not -empty / children). CSS-property guard, not a rendered-pixel test
    # — the real 4-per-screen look is checked on the wall.
    block = re.search(r"\.camera-page\s*\{([^}]*)\}", CSS)
    assert block, "no .camera-page grid block in styles.css"
    body = block.group(1)
    assert "grid-template-columns: 1fr 1fr" in body, \
        "camera page must be exactly two columns"
    assert "overflow-y: auto" in body, \
        "camera page must scroll so cameras beyond the first four are reachable"
    # The row track must be minmax(calc(... / 2), 1fr): the calc floor sizes the
    # peek, the 1fr ceiling fills the screen when there are four or fewer.
    assert re.search(r"grid-auto-rows:\s*minmax\(\s*calc\([^)]*\)\s*/\s*2\s*\)\s*,\s*1fr\s*\)", body), \
        "row height must be minmax(calc(... / 2), 1fr) — half-height floor + fill-when-few ceiling"
    assert "grid-auto-rows: 1fr" not in body, \
        "a bare grid-auto-rows: 1fr squashes every camera onto one screen"
    # No peek: the 2x2 must end exactly at the bottom edge. That requires the
    # bottom padding to equal the row gap (else the next row poked
    # padding-bottom - gap above the fold, e.g. a 6px hairline). Lock both.
    gap = re.search(r"gap:\s*(\d+)px", body)
    pad = re.search(r"padding:\s*\d+px\s+\d+px\s+(\d+)px", body)
    assert gap and pad and gap.group(1) == pad.group(1), \
        "camera-page bottom padding must equal the gap so exactly four fill the screen (no peek)"


def test_tab_bar_covers_all_tabs():
    index = (STATIC / "index.html").read_text()
    assert 'class="tabbar"' in index, "index.html lost the mobile tab bar"
    for tab in MOBILE_TABS:
        assert f'data-tab="{tab}"' in index, f"tab bar missing the {tab} tab"
    assert f'data-tab="{MOBILE_TABS[0]}"' in re.search(r"<body[^>]*>", index).group(0), \
        "body must start on the chores tab"


def test_swatch_palette_present():
    # SWATCHES moved from admin.js to common.js (shared with the Chores-page
    # inline people editor); the palette must still ship there in full.
    common = (STATIC / "common.js").read_text().lower()
    for hx in SWATCH_HEXES:
        assert hx.lower() in common, f"common.js is missing swatch hex {hx}"


def test_swatch_hexes_meet_dual_theme_contrast():
    """Person-color swatches are painted as name text, card border, and check
    fill directly on the card surface, which is #FFFFFF in light theme and
    #141A26 in dark theme (Task 1). Every offered hex must stay legible
    (WCAG contrast >= 3:1, the AA-large bar for the 18px bold name) against
    BOTH grounds, so nothing picked in the admin editor goes illegible when
    the wall's theme flips."""
    assert len(SWATCH_HEXES) == len(set(SWATCH_HEXES)), \
        "swatch palette has a duplicate hex"
    for hx in SWATCH_HEXES:
        cw = _contrast_ratio(hx, LIGHT_SURFACE)
        cd = _contrast_ratio(hx, DARK_SURFACE)
        assert cw >= MIN_CONTRAST, \
            f"{hx} contrast vs light surface {LIGHT_SURFACE} is {cw:.2f}, need >= {MIN_CONTRAST}"
        assert cd >= MIN_CONTRAST, \
            f"{hx} contrast vs dark surface {DARK_SURFACE} is {cd:.2f}, need >= {MIN_CONTRAST}"


# ------------------------------------------------- theme foundation (Task 1)
# Light is the base theme (bare :root); dark and accent are overrides layered
# under @media (prefers-color-scheme: dark) / [data-theme] / [data-accent].

_TOKEN_DEF = re.compile(r"(--[a-z0-9-]+)\s*:")


def _root_tokens():
    """Custom-property names defined on the bare `:root {…}` block (the light
    base). `:root[...]` and `:root:not(...)` selectors are excluded because the
    `[`/`:` after `:root` stops the `:root\\s*\\{` match."""
    m = re.search(r":root\s*\{([^{}]*)\}", CSS)
    assert m, "no bare :root block in styles.css"
    return set(_TOKEN_DEF.findall(m.group(1)))


def _override_tokens():
    """Tokens redefined inside any dark/accent override block (its selector
    mentions data-theme or data-accent, incl. the @media dark wrapper)."""
    toks = set()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        if "data-theme" in m.group(1) or "data-accent" in m.group(1):
            toks |= set(_TOKEN_DEF.findall(m.group(2)))
    return toks


def test_theme_tokens_base_on_root_no_override_orphans():
    """Every token an override block sets must also be defined on the bare
    :root light base: no load-bearing token exists ONLY inside a
    @media/[data-theme]/[data-accent] block. Proves light is the base."""
    root = _root_tokens()
    assert "--accent" in root and "--ink" in root, "expected theme tokens on :root"
    orphans = sorted(_override_tokens() - root)
    assert orphans == [], \
        f"tokens defined only in a dark/accent override, not on :root: {orphans}"


def test_display_controls_present_on_the_wall_gear():
    """The persisted theme picker ships in the wall's gear popover (the only
    display-control surface now that the admin page is retired): the five theme
    modes (Light/Soft/Blue=dark/Grey/Black), the four accents, and
    None/Wells/Lines columns. The blue-navy dark keeps its stored value "dark"
    (labelled "Blue") so no prefs migrate."""
    index = (STATIC / "index.html").read_text()
    for mode in ("light", "soft", "dark", "grey", "black"):
        assert f'data-theme-set="{mode}"' in index, f"missing the {mode} theme button"
    for accent in ("cyan", "violet", "amber", "green"):
        assert f'data-c="{accent}"' in index, f"missing the {accent} accent swatch"
    assert 'data-cols-set="none"' in index and 'data-cols-set="wells"' in index \
        and 'data-cols-set="lines"' in index
    # the wall must be re-themable without a phone: the gear + its popover
    assert 'id="wall-gear"' in index and 'id="theme-pop"' in index


def test_five_theme_token_blocks_defined_in_css():
    """Each selectable mode resolves its own token block. Light is the bare
    :root base; the other four are explicit [data-theme] blocks. "dark" is the
    unchanged blue-navy; soft/grey/black are the new modes."""
    for mode in ("soft", "dark", "grey", "black"):
        assert f':root[data-theme="{mode}"]' in CSS, f"missing token block for {mode}"


def test_non_light_modes_have_working_accent_overrides():
    """Every non-Light mode redefines --accent in its own [data-theme] block
    (specificity 0,2,0), which beats the base :root[data-accent] rules (also
    0,2,0, earlier in the file). So each such mode MUST carry its own per-accent
    overrides at 0,3,0 or the accent picker is dead in that mode: grey/black use
    dark-family values, soft uses light-family values. (Light is the bare :root,
    which the base :root[data-accent] rules already win over, so it needs none.)"""
    for accent in ("violet", "amber", "green"):
        for mode in ("dark", "grey", "black", "soft"):
            assert f':root[data-theme="{mode}"][data-accent="{accent}"]' in CSS, \
                f"{mode} missing the {accent} accent override (picker would be dead)"


def test_columns_control_offers_none_wells_and_lines():
    """All three mockup separation options ship (Lines was added back on request):
    each has a button on both surfaces AND consuming CSS so it visibly does
    something (no dead option)."""
    assert 'data-cols-set="lines"' in ALL_HTML, "the Lines column button must ship"
    assert ">Lines<" in ALL_HTML, "the Lines label must be present"
    assert 'data-cols="lines"' in CSS, "the Lines option must have consuming CSS"
    # the offered options are exactly none + wells + lines
    offered = set(re.findall(r'data-cols-set="([^"]+)"', ALL_HTML))
    assert offered == {"none", "wells", "lines"}, \
        f"columns offered {offered}, expected none+wells+lines"


def test_wells_column_separation_is_styled():
    """The Columns control must DO something: picking Wells wraps each column in
    a well. Pin that the consuming CSS exists (none is the flat default)."""
    assert 'data-cols="wells"' in CSS, "the Wells columns option has no visible effect"


# Properties that make an element the containing block for its position:fixed
# descendants. Putting ANY of these on .is-night (the <body>) unpins the fixed
# mobile tab bar + overlays so they scroll off at night. `filter` shipped the
# bug once; the rest cause the identical failure, so guard the whole class.
_CONTAINING_BLOCK_PROPS = {
    "filter", "transform", "perspective", "backdrop-filter",
    "will-change", "contain",
}


def _selector_subject(sel):
    """The element a selector actually styles = its last compound (after the
    final descendant/child/sibling combinator)."""
    return re.split(r"\s*[>~+]\s*|\s+", sel.strip())[-1]


def test_night_dim_never_makes_the_body_a_fixed_containing_block():
    """REGRESSION GUARD: the night dim (.is-night) must apply its dim to the
    body's CHILDREN, never to the .is-night element (the <body>) itself. Any of
    filter/transform/perspective/backdrop-filter/will-change/contain on an
    element makes it the containing block for its position:fixed descendants, so
    putting one on <body> unpins the fixed mobile tab bar and the full-screen
    overlays -- they scroll off with the page at night. This bug shipped once
    (filter on .is-night); guard the whole property class so it never returns.

    Robust to the ways a regression could hide: comments stripped, @media (and
    other at-rule) wrappers flattened (the mobile tab bar is the whole point, so
    a media query is the likeliest reintroduction site), and compound/pseudo/
    attribute forms (`.is-night.foo`, `.is-night:hover`, `body.is-night`) all
    caught by matching whether `.is-night` is the selector's SUBJECT."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)      # strip comments
    css = re.sub(r"@[\w-]+[^{}]*\{", "", css)            # flatten at-rule wrappers
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        selectors, block = m.group(1), m.group(2)
        props = {d.split(":", 1)[0].strip().lower()
                 for d in block.split(";") if ":" in d}
        if not (props & _CONTAINING_BLOCK_PROPS):
            continue
        for sel in selectors.split(","):
            subj = _selector_subject(sel)
            # `.is-night` present in the SUBJECT compound (not just a descendant
            # of it): `\b`-style boundary so `.is-nightly` is not a match.
            if re.search(r"\.is-night(?![\w-])", subj):
                bad = props & _CONTAINING_BLOCK_PROPS
                raise AssertionError(
                    f"{sorted(bad)} on the .is-night element (the <body>) makes "
                    "it a containing block for position:fixed descendants, "
                    "unpinning the mobile tab bar and overlays; apply the night "
                    "dim to its children (`.is-night > *`) instead. Offending "
                    f"selector: {sel.strip()!r}")
    # ...and the dim must still be applied to the children (tied to the actual
    # dim rule, whitespace-tolerant), or night mode silently stops dimming.
    assert re.search(r"\.is-night\s*>[^{}]*\{[^}]*(?:brightness|filter)", css), \
        "the night dim must still be applied to .is-night's children"


@pytest.mark.parametrize("html_path", HTML_FILES, ids=lambda p: p.name)
def test_theme_js_is_first_script(html_path):
    """theme.js stamps data-theme/data-accent/data-cols on <html> before first
    paint, so it must be the first <script> referenced in every page."""
    scripts = [s.split("?")[0] for s in
               re.findall(r'<script src="([^"]+)"', html_path.read_text())]
    assert scripts and scripts[0] == "theme.js", \
        f"{html_path.name}: theme.js must be the first script (got {scripts[:1]})"


def test_settings_has_a_features_group():
    assert ".integ-group-title" in CSS, "features/integrations sub-headers unstyled"
    hub = (STATIC / "hub.js").read_text()
    assert "'Features'" in hub or '"Features"' in hub, \
        "renderIntegrations must render a Features group"


def test_off_features_hide_their_wall_surface():
    assert re.search(r"body\.integ-off-chores[^\{]*\.people-col[^\{]*\{[^}]*display:\s*none",
                     CSS), "chores-off must hide the people column on the wall"
    assert re.search(r"body\.integ-off-todos[^\{]*\.todo-slot[^\{]*\{[^}]*display:\s*none",
                     CSS), "todos-off must hide the to-do slot on the wall"


def test_all_off_empty_state_present_and_wired():
    index = (STATIC / "index.html").read_text()
    assert 'id="hub-empty-msg"' in index, "missing all-off empty-state element"
    # Both load-bearing rules, not just the substring "body.hub-empty": the
    # grid/tabbar must actually hide AND the empty-state message must actually
    # show, or the page silently renders blank instead of the intended panel.
    assert re.search(
        r"body\.hub-empty\s+\.hub-grid\s*,\s*\n?\s*body\.hub-empty\s+\.tabbar\s*\{"
        r"[^}]*display:\s*none",
        CSS,
    ), "body.hub-empty must hide .hub-grid and .tabbar"
    assert re.search(
        r"body\.hub-empty\s+\.hub-empty-state\s*\{[^}]*display:\s*flex",
        CSS,
    ), "body.hub-empty must show .hub-empty-state"
    hub = (STATIC / "hub.js").read_text()
    assert "updateTabVisibility" in hub and "TAB_FEATURES" in hub, \
        "data-driven tab visibility missing"


def test_hidden_tab_button_is_actually_hidden():
    # b.hidden (the HTML `hidden` attribute) relies on the UA rule
    # [hidden]{display:none}, but that's beaten by the author rule
    # .tab-btn{display:flex} regardless of specificity (author origin beats
    # UA origin) — a "hidden" tab kept rendering flex on a real phone unless
    # an author-origin rule at >= .tab-btn's specificity wins it back.
    assert re.search(r"\.tab-btn\[hidden\][^\{]*\{[^}]*display:\s*none", CSS), \
        "hidden tab buttons must be display:none (author rule beats UA [hidden])"


def test_wall_grid_reflows_on_toggle():
    hub = (STATIC / "hub.js").read_text()
    assert "applyWallLayout" in hub, "wall grid reflow missing"
    assert "gridTemplateAreas" in hub, "reflow must rebuild grid-template-areas"


def test_laundry_card_static_guards():
    """The laundry card's load-bearing wiring: the off-toggle hides its slot,
    the phone Weather tab counts laundry as a backing feature, and the tumble
    animation respects prefers-reduced-motion (the wall's only other ambient
    motion, the sky, holds the same bar)."""
    assert re.search(r"body\.integ-off-laundry[^\{]*#laundry-slot[^\{]*\{[^}]*display:\s*none",
                     CSS), "laundry-off must hide the laundry slot"
    hub = (STATIC / "hub.js").read_text()
    assert re.search(r"laundry:\s*\['laundry'\]", hub), \
        "laundry must back its own phone tab (TAB_FEATURES)"
    assert re.search(
        r"prefers-reduced-motion[^}]*\{[^}]*\.ln-tumble[^\{]*\{[^}]*animation:\s*none",
        CSS, re.S), "the tumble must stop under prefers-reduced-motion"
    # the timer arc + tumble only exist inside .ln-door SVG markup built by
    # lnPortholeSvg; the renderer must be wired into the poll loop
    assert "fetchLaundry" in hub and "renderLaundry" in hub and "laundryTick" in hub
    # the phone Laundry tab shows ONLY the laundry slot from the shared
    # .panels section, and the Weather tab no longer duplicates it
    assert re.search(r'body\[data-tab="laundry"\] \.panels > :not\(\.laundry-slot\)'
                     r'[^{]*\{[^}]*display:\s*none', CSS), \
        "laundry tab must filter .panels to the laundry slot"
    assert re.search(r'body\[data-tab="weather"\] \.laundry-slot[^{]*\{[^}]*display:\s*none',
                     CSS), "weather tab must not duplicate the laundry slot"
