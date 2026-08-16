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


MOBILE_TABS = ("chores", "cal", "cams", "weather")

# the wall sections that each live in the hub grid; a tab shows one and hides
# the rest. (.camgrid is not here — it is default-hidden and only the cams tab
# reveals it.)
WALL_SURFACES = {"people-col", "cal", "tiles", "panels"}

# each tab's surface, by the section class it leaves visible. The cams tab shows
# the .camgrid (a single stacked column on the phone) and suppresses the wall's
# .tiles column entirely, so its surface is none of the shared wall sections.
TAB_SURFACE = {"chores": ".people-col", "cal": ".cal",
               "cams": ".camgrid", "weather": ".panels"}


def test_mobile_reflow_block_present():
    """Below 1000px the wall page reflows to a phone layout with bottom tabs
    (operator request 2026-08-13); the fixed 1920 canvas stays above it."""
    assert "@media (max-width: 1000px)" in CSS, "mobile breakpoint missing"
    for tab in MOBILE_TABS:
        assert f'body[data-tab="{tab}"]' in CSS, \
            f"missing mobile visibility rules for the {tab} tab"


def test_each_tab_hides_every_other_surface():
    """Four tabs, one surface each (operator request 2026-08-13): a tab's
    rule must hide the other three sections, and never its own."""
    mobile = CSS[CSS.index("@media (max-width: 1000px)"):]
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
