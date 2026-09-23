"""Frontend contract tests — no browser, no server.

The wall page has a JS runner for its pure helpers (test_js.py); these pin the
file-level conventions the design spec is built on: every referenced class is
styled, the house-climate palette tokens are present, scripts load in order,
and — because the three full-screen iframe URLs come from the API at runtime —
no literal http(s) URL appears anywhere in the committed frontend (a LAN wall
display reaches nothing on the internet).
"""
import json
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "family_hub" / "web" / "static"

CSS = (STATIC / "styles.css").read_text()
HTML_FILES = sorted(STATIC.glob("*.html"))
JS_FILES = sorted(STATIC.glob("*.js"))
ALL_HTML = "\n".join(p.read_text() for p in HTML_FILES)
ALL_JS = "\n".join(p.read_text() for p in JS_FILES)
OSK = (STATIC / "osk.js").read_text()
HUB = (STATIC / "hub.js").read_text()
APP_PY = (STATIC.parents[1] / "app.py").read_text()
CONFIG_PY = (STATIC.parents[1] / "config.py").read_text()
COMMON = (STATIC / "common.js").read_text()

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
               "f-person", "f-rotadd", "f-rotation", "f-rothint", "f-error",
               # routine-type + reminder-times hooks (styling rides sibling
               # classes: .segmented / .interval-row / .txt-input / .time-list)
               "f-weekfreq", "f-interval", "f-intervaldays", "f-timeinput",
               "f-timeadd", "f-times"}


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
    # _css_rule concatenates the base rule and the wall override (below) in file
    # order, so this first min-height match is the base (phone) rule — the one
    # that must stay >= 48. The wall override is checked separately.
    rule = _css_rule(".chore-row")
    m = re.search(r"min-height:\s*(\d+)px", rule)
    assert m and int(m.group(1)) >= 48, ".chore-row must be >= 48px (tap target)"


def test_wall_chore_row_stays_a_comfortable_row():
    # The wall is mouse-only, so a min-width:1001px override compacts chore rows
    # below the 48px phone touch floor to fit more of the family (operator,
    # 2026-08-24: "compact a little so more fits on my hp screen"). Pin a floor
    # so a later "make it tighter" can't collapse the rows to an unreadable
    # sliver, and confirm the compaction is a WALL-scoped override (min-width),
    # never an edit to the base rule the phone shares.
    m = re.search(r"@media\s*\(min-width:\s*1001px\)\s*\{(.*?)\n\}", CSS, re.S)
    assert m, "expected a min-width:1001px wall-density block in styles.css"
    block = m.group(1)
    cm = re.search(r"\.chore-row\s*\{[^}]*min-height:\s*(\d+)px", block)
    assert cm, "the wall block must set a compacted .chore-row min-height"
    assert 40 <= int(cm.group(1)) < 48, \
        "wall chore row must compact (< 48px) yet stay comfortable (>= 40px)"


def test_wall_todo_row_stays_a_comfortable_row():
    # Same wall-only density treatment as the chore rows: the to-do rows compact
    # below their guarded 52px phone touch floor on the mouse-only wall (operator,
    # 2026-08-24). Pin a comfortable floor and confirm it's the wall-scoped
    # override, not an edit to the base rule the phone shares.
    m = re.search(r"@media\s*\(min-width:\s*1001px\)\s*\{(.*?)\n\}", CSS, re.S)
    assert m, "expected a min-width:1001px wall-density block in styles.css"
    block = m.group(1)
    tm = re.search(r"\.todo-row-full\s*\{[^}]*min-height:\s*(\d+)px", block)
    assert tm, "the wall block must set a compacted .todo-row(-full) min-height"
    assert 40 <= int(tm.group(1)) < 52, \
        "wall to-do row must compact (< 52px) yet stay comfortable (>= 40px)"


def test_weather_forecast_strip_classes_are_styled():
    # The 5-day strip's visible classes must all be styled, so a rename in hub.js
    # (wxForecastHtml / wxGlyph) can't ship an unstyled, misaligned strip — same
    # guard pattern as the chore-form controls below.
    for cls in (".wx-forecast", ".wf-day", ".wf-lbl", ".wf-today", ".wf-temps",
                ".wf-hi", ".wf-lo", ".wx-glyph", ".wg-cloud", ".wg-sun"):
        assert _css_rule(cls).strip(), f"{cls} (forecast strip) is unstyled"


def test_chore_form_schedule_and_reminder_controls_are_styled():
    # The routine-type + reminder-times controls added to the shared chore form.
    # Pin each visible class is actually styled so the editor can't render the
    # interval stepper / week-cadence toggle / time chips unstyled.
    for cls in (".interval-row", ".interval-word", ".interval-num",
                ".time-add", ".time-input", ".time-add-btn",
                ".time-list", ".time-chip"):
        assert _css_rule(cls).strip(), f"{cls} (chore form control) is unstyled"


def test_chore_form_time_and_number_inputs_avoid_ios_zoom_and_are_tap_targets():
    # The <input type=time> (reminder add) and <input type=number> (every-N-days)
    # both ride the shared .txt-input box, which MUST stay >= 16px font (or iOS
    # Safari zooms the page on focus) and >= 44px tall (tap target, CLAUDE.md).
    box = _css_rule(".txt-input")
    fs = re.search(r"font-size:\s*(\d+)px", box)
    assert fs and int(fs.group(1)) >= 16, ".txt-input font must be >= 16px (no iOS zoom-on-focus)"
    ht = re.search(r"height:\s*(\d+)px", box)
    assert ht and int(ht.group(1)) >= 44, ".txt-input must be a >= 44px tap target"
    # Both new inputs must carry txt-input so they inherit that box (a width-only
    # override that dropped it would reintroduce the zoom/tap bug).
    for cls in ("interval-num", "time-input"):
        assert re.search(rf'class="txt-input[^"]*\b{cls}\b', ALL_JS), \
            f".{cls} must be applied alongside .txt-input"
    # The Add-time button and the time chips are their own tap targets.
    for cls in (".time-add-btn", ".time-chip"):
        rule = _css_rule(cls)
        m = re.search(r"(?:min-)?height:\s*(\d+)px", rule)
        assert m and int(m.group(1)) >= 44, f"{cls} must be a >= 44px tap target"


def test_person_editor_icloud_list_picker_and_badge():
    # The person→iCloud-list picker is a .txt-input <select> (so it inherits the
    # guarded >=16px/>=44px box — no iOS zoom-on-focus, real tap target), and the
    # "mirrored" badge shown next to a mapped person is styled.
    assert '<select class="txt-input" data-plist>' in ALL_JS, \
        "the iCloud-list picker must be a .txt-input <select> (>=16px font / >=44px tall)"
    assert _css_rule(".padmin-badge").strip(), ".padmin-badge (iCloud mirror tag) is unstyled"


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


def test_todo_digest_card_tier_classes_are_styled():
    # The wall To-Do card is a 3-tier digest (todoDigest/todoCardHtml in hub.js):
    # a labelled head per tier, its open count, and a "+N more" tap-through. Pin
    # that each class is styled so the digest can't render unstyled, and that the
    # "+N more" control clears the ~44px tap floor (CLAUDE.md) like the rows.
    for cls in (".todo-grp-head", ".todo-grp-count", ".todo-more"):
        assert _css_rule(cls).strip(), f"{cls} (To-Do digest card) is unstyled"
    more = _css_rule(".todo-more")
    mm = re.search(r"min-height:\s*(\d+)px", more)
    assert mm and int(mm.group(1)) >= 44, ".todo-more must be a >= 44px tap target"


def test_reminder_bucket_list_reuses_the_shared_row_and_box_classes():
    # The iCloud reminders view is built from the same .todo-row(-full)/.card
    # chrome as the local list — not a parallel styling system that could drift.
    # Pin that the source-chip + stacked-list classes it DOES add are styled.
    for cls in (".shead-chip", ".rem-list", ".rem-due", ".rem-pri"):
        assert _css_rule(cls).strip(), f"{cls} (iCloud reminders view) is unstyled"


def test_celebration_is_reduced_motion_guarded():
    assert ".card-celebrate" in CSS, "missing the completion celebration class"
    assert "prefers-reduced-motion" in CSS, "celebration must be reduced-motion guarded"


def test_cloud_drift_exit_is_container_relative():
    # The cloud drift must exit relative to the ACTUAL sky width, not a fixed px.
    # A fixed-px exit (translateX(420px)) cleared the 338px desktop column but
    # left the cloud mid-sky on the wider mobile/full-screen weather view, so it
    # snapped back on-screen — the visible "crazy reset". The fix: .sky is a
    # query container and the keyframe exits at 100cqw (+ margin), off-screen at
    # any width. Don't regress either half.
    # `\.sky\s*\{` (not `\.sky\b`) binds this to the bare `.sky {` rule — `\b`
    # would also match `.sky-cloud {`, `.sky-sun {`, etc., letting container-type
    # on the wrong selector satisfy it falsely (cqw would then resolve against
    # the wrong ancestor).
    assert re.search(r"\.sky\s*\{[^{}]*container-type\s*:\s*inline-size",
                     CSS), ".sky must be a query container (container-type: inline-size)"
    drift = re.search(r"@keyframes\s+drift\s*\{(?:[^{}]|\{[^{}]*\})*\}", CSS)
    assert drift, "missing @keyframes drift"
    to = re.search(r"\bto\s*\{[^{}]*\}", drift.group(0))
    assert to and "cqw" in to.group(0), \
        "cloud drift 'to' must exit at a container-relative width (100cqw), " \
        "not a fixed px that re-breaks the wide sky"
    # The "reset happens off-screen" promise also relies on .sky clipping — drop
    # overflow:hidden and a cloud driving to 100cqw+20px spills visibly past the
    # right edge instead of vanishing.
    assert re.search(r"\.sky\s*\{[^{}]*overflow\s*:\s*hidden", CSS), \
        ".sky must keep overflow:hidden so the off-screen drift exit stays clipped"


def test_sky_loops_are_not_phased_by_fixed_css_delays():
    # The weather card re-renders every 60s, restarting each sky animation from
    # its delay. A fixed CSS animation-delay on a sky loop snapped the clouds
    # back to the same spot every minute; skySceneHtml stamps a wall-clock phase
    # inline instead (SKY_LOOPS in hub.js). A CSS delay on an element rule would
    # only be overridden by that inline style, but on the rain/snow
    # pseudo-elements the CSS rule IS the delay, so it must read the --ph vars.
    for rule in re.finditer(r"\.sky-(?:cloud|sun|stars|fog|snow|rain)[^{}]*\{([^{}]*)\}", CSS):
        body = rule.group(1)
        for d in re.findall(r"animation-delay\s*:\s*([^;}]+)", body):
            assert d.strip().startswith("var(--ph-"), \
                f"sky loop has a fixed animation-delay ({d.strip()}); phase it from SKY_LOOPS instead"
        # a delay can also hide in the shorthand as a SECOND time value
        for a in re.findall(r"animation\s*:\s*([^;}]+)", body):
            times = re.findall(r"-?[\d.]+m?s\b", a)
            assert len(times) <= 1, \
                f"sky loop shorthand carries a fixed delay ({a.strip()}); phase it from SKY_LOOPS instead"
    for sel, var in ((r"\.sky-rain::before", "--ph-a"), (r"\.sky-rain::after", "--ph-b"),
                     (r"\.sky-snow::before", "--ph-a"), (r"\.sky-snow::after", "--ph-b")):
        # anchor on the rule's own selector (after a `}` or line start), so the
        # shared `.sky-rain::before, .sky-rain::after {` rule can't match
        m = re.search(r"(?:^|\})\s*" + sel + r"\s*\{([^{}]*)\}", CSS, re.M)
        assert m and re.search(r"animation-delay\s*:\s*var\(" + var, m.group(1)), \
            f"{sel} must take its phase from var({var})"


def test_week_strip_state_classes_are_styled():
    for cls in (".ws-done", ".ws-partial", ".ws-none", ".ws-rest", ".ws-away"):
        assert _css_rule(cls).strip(), f"{cls} week-strip state is unstyled"


def test_wall_renders_away_state():
    # F8: the old `"away" in js.lower()` was near-vacuous. Pin the actual class
    # the away branch emits AND that it carries a non-empty CSS rule, so a real
    # regression (renamed class, dropped style) is caught.
    assert "away-badge" in ALL_JS, "the away branch must render the away-badge"
    assert _css_rule(".away-badge").strip(), ".away-badge is unstyled"
    # the 5th week-strip state (away) must also be a styled cell
    assert _css_rule(".ws-away").strip(), ".ws-away (away week cell) is unstyled"


def test_wall_surfaces_away_overlay_failure():
    """S1b: when the server flags away_ok=false, the wall shows an unobtrusive
    note instead of silently rendering a genuinely-away person as present. Pin
    the flag check and the note text in hub.js (the DOM test proves it renders)."""
    assert "away_ok" in ALL_JS, "renderPeople must read the away_ok degraded flag"
    assert "away status unavailable" in ALL_JS, "the degraded-state note text"


def test_no_utc_today_fallback_in_hub():
    """The 'today' fallback must use local-midnight math (todayISO()), never
    `new Date().toISOString().slice(0, 10)` — toISOString() is UTC and rolls to
    tomorrow on a US evening, opening the calendar on the wrong month and marking
    the wrong 'today'. Every other date routine in the file is deliberately
    tz-local; keep the fallbacks in line."""
    assert "toISOString().slice(0, 10)" not in HUB and "toISOString().slice(0,10)" not in HUB, \
        "hub.js must not derive a local 'today' from UTC toISOString()"


def test_calendar_window_chain_stays_consistent():
    """The synced-window size is ONE number spread over four files, and every
    link is silent when it breaks:

        config window  >=  frontend fetch  <=  API ceiling

    Too big a fetch and /api/calendar 422s the wall's calendar on every load (an
    empty overlay, not a loud failure). Too small a fetch, or a config window
    below it, and `_calendar_block` caps the reported window back down, so the
    month view hatches "not synced" over days that ARE cached — the original
    complaint this window-widening fixed. Both directions must be pinned: a
    one-sided `fetch <= ceiling` guard passes happily with the fetch reverted to
    days=1. Checks EVERY fetch in the file, not just the first."""
    fetches = re.findall(r"/api/calendar\?days=(\d+)&past=(\d+)", HUB)
    assert fetches, "hub.js must fetch /api/calendar with explicit days/past"

    cap = re.search(r"^CAL_MAX_DAYS = (\d+)", APP_PY, re.M)
    assert cap, "app.py must define CAL_MAX_DAYS"
    ceiling = int(cap.group(1))

    # The server NAMES the window the wall fetches, and hub.js must ask for
    # exactly it. Equality, not `<=`: a one-sided bound is happily satisfied by a
    # fetch reverted to days=90, which is silent and is the original bug.
    want = re.search(r"^CAL_FETCH_DAYS = (\d+)", APP_PY, re.M)
    want_past = re.search(r"^CAL_FETCH_PAST = (\d+)", APP_PY, re.M)
    assert want and want_past, "app.py must name the wall's fetch window"
    want_days, want_back = int(want.group(1)), int(want_past.group(1))

    for raw_days, raw_past in fetches:
        assert (int(raw_days), int(raw_past)) == (want_days, want_back), (
            f"hub.js fetches {raw_days}/{raw_past} but app.py names "
            f"{want_days}/{want_back}; a fetch below the configured window "
            "silently caps the reported window and hatches cached days")

    assert want_days <= ceiling, \
        f"the wall fetches {want_days} days; /api/calendar rejects past {ceiling}"
    assert want_back <= ceiling, \
        f"the wall fetches {want_back} past days; /api/calendar rejects past {ceiling}"

    # Every shipped config must cover the fetch — below it, the reported window
    # caps to config and days that ARE cached hatch. config.demo.json counts:
    # the demo wall is the README screenshot and every visual gate.
    for name in ("config.example.json", "config.demo.json"):
        shipped = json.loads((STATIC.parents[3] / name).read_text())
        assert shipped["calendar_window_days"] >= want_days, \
            f"{name} syncs {shipped['calendar_window_days']} days, under the {want_days} fetched"
        assert shipped["calendar_past_days"] >= want_back, \
            f"{name} syncs {shipped['calendar_past_days']} past days, under the {want_back} fetched"

    # The dataclass defaults serve any config.json that omits the key; below the
    # fetch they silently hatch the difference on an otherwise healthy install.
    for field, floor in (("calendar_window_days", want_days),
                         ("calendar_past_days", want_back)):
        dflt = re.search(rf"^    {field}: int = (\d+)", CONFIG_PY, re.M)
        assert dflt, f"config.py must define a {field} default"
        assert int(dflt.group(1)) >= floor, \
            f"config.py defaults {field} to {dflt.group(1)}, under the {floor} fetched"


def test_calendar_full_view_claims_nothing_when_no_window_is_known():
    """A SPELLING ratchet, not coverage — don't mistake it for one.

    The behavior is pinned by `openOverlay("calendar"): the first paint never
    renders an unknown month as free` in tests/js/hub-dom.test.mjs, which drives
    the real entry point and COUNTS what the grid marks. Source-string
    assertions cannot see the two mutations that matter: moving the seed below
    the paint, and renaming the fallback variable while leaving the old string
    in a comment. Both restore the confident-lie behavior with every string here
    intact. This only keeps the load-bearing pieces from being deleted outright.
    """
    assert "|| emptyWindow(todayStr)" in HUB, \
        "renderCalFull must fall back to emptyWindow(), never to an unknown window"
    assert "const win = calWin && calWin.window;" not in HUB, \
        "the bare fail-open fallback is the bug; it must not come back"
    assert "loading_full: true" in HUB, \
        "the seeded first paint must be flagged in-flight, or its narrow " \
        "home-feed window reads as the final answer"


def test_calendar_overlay_opens_on_the_layout_default_view():
    """The calendar overlay picks its opening view from calDefaultMode() (Week
    agenda on a phone, month grid on the wall) rather than hard-coding 'month' —
    a phone-width month grid hides every event title. Pin the wiring so a future
    edit can't silently revert to the always-month default."""
    assert "calState.mode = calDefaultMode();" in HUB, \
        "the calendar overlay must open on calDefaultMode(), not a fixed 'month'"
    assert "matchMedia('(max-width: 1000px)')" in HUB, \
        "calDefaultMode must mirror the CSS phone breakpoint exactly"


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
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _phone_shell_css()):
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
    # The body must be a FIXED box pinned to every viewport edge: the browser
    # sizes position:fixed + inset:0 to the layout viewport on every layout with
    # no script involved. Both earlier approaches went stale on iOS (100dvh
    # after a bfcache restore, then a JS-measured --app-h var after a discarded
    # -tab reload in iOS Chrome) and left the tab bar floating over a black gap
    # until a manual reload. No height var, no dvh, no measurement to go stale.
    assert "position:fixed" in body and "inset:0" in body, \
        "phone body must be a position:fixed inset:0 box (viewport-pinned shell)"
    assert "height:auto" in body, \
        "phone body height must be auto (sized by the fixed insets), never a length or viewport unit"
    assert "min-height:0" in body, \
        "phone body must clear the base min-height:100vh floor (min-height:0), " \
        "or the tab bar drops below the fold on iOS"
    # The measurement must not creep back through ANY door: no --app-h var in
    # the CSS or hub.js (comments stripped), and no vh/dvh/svh/lvh body height.
    css_code = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    hub_code = re.sub(r"/\*.*?\*/|//[^\n]*", "", HUB, flags=re.S)
    assert "--app-h" not in css_code and "--app-h" not in hub_code, \
        "the phone shell must not depend on a measured height var (it went stale on iOS)"
    # Every body-matching rule in the shell block, not just the exact one above:
    # a later, more specific rule (a data-tab variant, the narrow inner media
    # block) could override height/position/inset and re-open the gap. And a
    # transform/filter/etc. on <body> would make it the containing block for the
    # fixed overlays and toast, re-anchoring them (why night dim filters the
    # CHILDREN of body).
    body_rules = [(_ns(m.group(1)), _ns(m.group(2)))
                  for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _phone_shell_css())
                  if re.search(r"(^|[\s>+~,)])body(?![\w-])", m.group(1))]
    assert body_rules, "no body rules found in the phone-shell block"
    for sel, decl in body_rules:
        for m in re.finditer(r"(?<![\w-])height:([^;]*)", decl):
            assert m.group(1) in ("auto", "0"), \
                f"{sel}: body height must stay auto (found {m.group(1)!r}); " \
                "a fixed/viewport-unit height is the stale-gap bug returning"
        for m in re.finditer(r"(?<![\w-])position:([^;]*)", decl):
            assert m.group(1) == "fixed", f"{sel}: body position must stay fixed"
        for bad in ("transform:", "filter:", "zoom:", "perspective:", "contain:", "will-change:"):
            assert bad not in decl, \
                f"{sel}: {bad} on <body> makes it the containing block for the fixed overlays"
    # The base (wall) body rule applies on the phone too.
    base_body = _ns(re.search(r"\nbody\s*\{([^{}]*)\}", css_code).group(1))
    for bad in ("transform:", "filter:", "zoom:", "perspective:", "contain:", "will-change:"):
        assert bad not in base_body, f"base body rule: {bad} would re-anchor the fixed overlays"
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
    for cls in (".mgrid", ".mg-week", ".mg-day", ".mg-today", ".mg-ev", ".mg-bar",
                ".mg-bar-contr", ".mg-bar-contl", ".mg-more", ".cal-ev-allday", ".cal-daynum",
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


def test_home_screen_manifest_and_apple_meta():
    """iOS Chrome tab-bar gap (#45/#53/#80): the reliable chrome-less launch is
    Safari's Add to Home Screen, which needs a standalone web-app manifest plus
    the apple-mobile-web-app meta tags and a touch icon. Guard the whole set —
    losing any one silently drops back to a browser-chrome launch, re-opening
    the very toolbar-inset bug this change closes."""
    index = (STATIC / "index.html").read_text()
    assert 'rel="manifest" href="manifest.webmanifest' in index, \
        "index.html lost the web-app manifest link"
    assert 'name="apple-mobile-web-app-capable" content="yes"' in index, \
        "index.html lost apple-mobile-web-app-capable (no standalone launch)"
    assert 'name="apple-mobile-web-app-title"' in index
    assert 'rel="apple-touch-icon"' in index, "index.html lost the apple-touch-icon"
    # black, NOT black-translucent: translucent pulls content under the status
    # bar and re-opens the top-inset ambiguity this change is ending.
    assert 'content="black-translucent"' not in index, \
        "status-bar-style must not be black-translucent (top-inset regression)"

    mani = STATIC / "manifest.webmanifest"
    assert mani.exists(), "manifest.webmanifest is missing"
    data = json.loads(mani.read_text())
    assert data.get("display") == "standalone", \
        "manifest must be display:standalone (that is the chrome-less launch)"
    # every icon the manifest names, plus the apple-touch-icon, must exist on disk
    named = {ic["src"] for ic in data.get("icons", [])}
    named.add("apple-touch-icon.png")
    for src in named:
        assert (STATIC / src).exists(), f"manifest/apple icon missing on disk: {src}"


def test_tabbar_gap_self_heal_is_wired():
    """The self-heal reload (hub.js) is the load-bearing fix for the hub opened
    in a normal Chrome tab. Guard that it is actually booted and that its two
    safety rails — the one-shot reload guard and the input-active hold — are
    present, so a refactor can't silently disarm it into a reload loop or a
    yank-while-typing."""
    hub = (STATIC / "hub.js").read_text()
    assert "initTabbarGapSelfHeal()" in hub, "self-heal is defined but never booted"
    assert "location.reload()" in hub, "self-heal lost its reload — it can't heal"
    assert "fh-selfheal-tried" in hub, "lost the one-shot guard — risks a reload loop"
    assert "hold:input-active" in hub, "lost the never-reload-while-typing guard"
    assert "SELFHEAL_MIN_SHORTFALL_PX" in hub, "lost the shortfall threshold constant"
    # the diagnostics endpoint the wake telemetry posts to
    assert "/api/diag/viewport" in hub, "self-heal lost its diagnostics report"


def test_admin_has_away_controls():
    """Away mode is admin-only, set from the inline people-management section
    (peopleAdminHtml in hub.js), not a wall-facing control — see
    test_admin_html_is_retired for why there's no separate admin surface at
    all. Guard the hooks the away endpoints and click-wiring depend on."""
    # F9: these substring checks are a coarse smoke test -- the REAL guard for
    # the away admin controls is the executable DOM suite (tests/js/*.mjs), which
    # renders peopleAdminHtml and clicks the hooks end-to-end. Keep these as a
    # cheap "the wiring didn't vanish" tripwire.
    assert "/api/admin/away" in ALL_JS
    assert "data-paway" in ALL_JS       # per-person "Going away" button hook
    assert "data-pback" in ALL_JS       # "I'm back" (closes an open period)
    assert "data-paway-all" in ALL_JS   # "Pause everyone" header button


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
    # the slot div is built UNCONDITIONALLY: buildPanels runs once per page
    # life, so gating the div on build-time availability froze a transient
    # server-side outage into a missing card until a manual refresh (live
    # board, 2026-08-17). Availability is renderLaundry's per-poll decision.
    assert re.search(r"function laundrySlotHtml\(\) \{\s*\n?\s*return `<div class=\"laundry-slot\"",
                     hub), "laundrySlotHtml must build the slot unconditionally"
    m = re.search(r"function renderLaundry\(", hub)
    assert m and re.search(r"\.some\(\(i\) => i\.id === 'laundry'\)",
                           hub[m.start():m.start() + 1600]), \
        "renderLaundry must re-check integration availability per render"
    # the REAL-TIME lane: the wall holds an SSE subscription to the server's
    # background watcher, so any status change reaches the card in seconds —
    # everywhere in the cycle, not just near a projected finish (the old
    # endgame fast lane this replaced). Losing any of this quietly demotes
    # the card back to 60s-poll latency with every visible feature intact.
    assert "new EventSource('/api/laundry/stream'" in hub, \
        "the laundry card must subscribe to the live stream"
    # both feeds converge on ONE applier (render-on-change discipline): the
    # POLL body and the STREAM handler must each route through applyLaundry —
    # a bare count would pass on the definition + one caller, letting the
    # other feed drift onto its own render path
    assert re.search(r"function applyLaundry\(", hub), "applyLaundry missing"
    fetch_body = re.search(r"async function fetchLaundry\(\) \{(.*?)\n\}",
                           hub, re.S)
    assert fetch_body and "applyLaundry(" in fetch_body.group(1), \
        "fetchLaundry must feed applyLaundry"
    onmsg = re.search(r"\.onmessage = \(ev\) => \{(.*?)\n    \};", hub, re.S)
    assert onmsg and "applyLaundry(" in onmsg.group(1), \
        "the stream handler must feed applyLaundry"
    # the wall kiosk never fires a wake event, and EventSource does NOT
    # auto-retry an HTTP-level failure (a 502 mid-deploy closes it for
    # good) — the poll beat must re-arm the stream or the kiosk silently
    # loses real-time forever after one bad deploy window
    assert re.search(r"setInterval\(lnConnect, POLL_MS\)", hub), \
        "the poll beat must re-arm a CLOSED stream (setInterval lnConnect)"
    assert re.search(r"\.onerror = ", hub[hub.index("function lnConnect"):]
                     [:1500]), "a dropped stream must at least log (onerror)"
    # ... and the lane must actually OPEN at boot: lnConnect defined but
    # never called leaves the whole feature dark with every guard green
    assert re.search(r"^lnConnect\(\);", hub, re.M), \
        "bootstrap must call lnConnect()"
    # iOS suspends EventSource in background tabs and never resumes it —
    # every wake path must reconnect AND refetch (the stream greeting covers
    # new state, the fetch covers a wall whose stream died mid-suspend)
    assert re.search(r"function lnConnect\(", hub), "lnConnect missing"
    wake_body = re.search(r"function lnWake\(\) \{(.*?)\n\}", hub, re.S)
    assert wake_body and "lnConnect()" in wake_body.group(1) \
        and "fetchLaundry()" in wake_body.group(1), \
        "lnWake must reconnect the stream AND refetch"
    for ev in ("pageshow", "visibilitychange"):
        assert re.search(rf"addEventListener\('{ev}'[^\n]*\n?[^\n]*lnWake",
                         hub), f"lnWake must run on {ev}"
    # the endgame fast lane is GONE — a half-restored copy would fight the
    # stream (stacked timers) without anyone noticing
    for gone in ("lnEndgame", "LN_FAST_POLL_MS", "LN_ENDGAME_AHEAD_MIN",
                 "LN_ENDGAME_BEHIND_MIN", "lnFastTimer"):
        assert gone not in hub, f"{gone} should be fully retired"
    # every animated laundry class must be neutralized under reduced motion
    # (a class dropped from the block would leave the full 12.8s tumble
    # running for reduced-motion users with no test failing)
    rm_block = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", CSS, re.S)
    assert rm_block, "prefers-reduced-motion block missing"
    # exact-token match, not substring: ".ln-sud" would otherwise ride on
    # ".ln-sud-drift" (and ".ln-heat" on ".ln-heatglow"), leaving the
    # shorter class removable with no test failing
    for cls in ("ln-tumble", "ln-flyp", "ln-lift", "ln-heap",
                "ln-water", "ln-waterline", "ln-sud", "ln-sud-drift",
                "ln-heatglow", "ln-halo"):
        assert re.search(rf"\.{re.escape(cls)}(?![\w-])", rm_block.group(1)), \
            f"reduced-motion must neutralize .{cls}"
    # a paused machine must LOOK paused: the freeze rule (the one whose
    # declaration block STARTS with animation-play-state, unlike the done/
    # reduced-motion rules) must cover every animated class. ln-sud-drift
    # needs no entry — drift bubbles carry ln-sud in the markup, and the
    # two-class paused selector out-specifies the drift animation rule.
    paused = re.search(r"([^{}]+)\{\s*animation-play-state:\s*paused", CSS)
    assert paused, "paused-phase freeze rule missing"
    for cls in ("ln-tumble", "ln-flyp", "ln-lift", "ln-water",
                "ln-waterline", "ln-sud", "ln-heatglow", "ln-heap"):
        assert re.search(rf"\.ln-ph-paused \.{re.escape(cls)}(?![\w-])",
                         paused.group(1)), \
            f"paused phase must freeze .{cls}"
    # the phone Laundry tab shows ONLY the laundry slot from the shared
    # .panels section, and the Weather tab no longer duplicates it
    assert re.search(r'body\[data-tab="laundry"\] \.panels > :not\(\.laundry-slot\)'
                     r'[^{]*\{[^}]*display:\s*none', CSS), \
        "laundry tab must filter .panels to the laundry slot"
    assert re.search(r'body\[data-tab="weather"\] \.laundry-slot[^{]*\{[^}]*display:\s*none',
                     CSS), "weather tab must not duplicate the laundry slot"


def test_fleet_card_static_guards():
    """The fleet card's load-bearing wiring: the off-toggle hides its slot,
    the slot is built unconditionally (laundry's 2026-08-17 lesson), the poll
    loop is wired at boot, and the Console full-screen button is config-gated
    (never a dead button)."""
    assert re.search(r"body\.integ-off-fleet[^\{]*#fleet-slot[^\{]*\{[^}]*display:\s*none",
                     CSS), "fleet-off must hide the fleet slot"
    hub = (STATIC / "hub.js").read_text()
    assert re.search(r"function fleetSlotHtml\(\) \{\s*\n?\s*return `<div class=\"fleet-slot\"",
                     hub), "fleetSlotHtml must build the slot unconditionally"
    assert "fetchFleet" in hub and "renderFleet" in hub and "fleetCardHtml" in hub
    assert re.search(r"^fetchFleet\(\);", hub, re.M), "bootstrap must call fetchFleet()"
    assert re.search(r"setInterval\(fetchFleet, POLL_MS\)", hub), \
        "the poll beat must re-fetch fleet on the hub cadence"
    # buildPanels must give fleet a native slot, never an always-on iframe
    # embed, even when a 'fleet' links.panels entry exists (that entry is
    # full-screen-only — the Console button's URL, nothing more)
    m = re.search(r"function buildPanels\(\) \{(.*?)\n\}", hub, re.S)
    assert m and "fleetSlotHtml()" in m.group(1), \
        "buildPanels must append the fleet slot unconditionally"
    assert re.search(r"if \(p\.id === 'fleet'\) return '';", m.group(1)), \
        "a configured fleet panels entry must not get an always-on iframe embed"
    # renderFleet's Console button is gated on a 'fleet' links.panels entry
    # existing — an unconfigured full dashboard must never offer a dead link
    rf = re.search(r"function renderFleet\(", hub)
    assert rf and re.search(r"links\.panels\.some\(\(p\) => p && p\.id === 'fleet'\)",
                            hub[rf.start():rf.start() + 1200]), \
        "renderFleet must gate the Console button on a configured 'fleet' panel"
    # no animation loop on this card by design (plain width-update progress
    # bar) — confirm no stray keyframes/transition slipped in unguarded
    assert "fleet-bar-fill { height: 100%; border-radius: 999px; background:" in CSS
    # the wall places fleet directly under laundry in the panels column
    # (owner's placement intent); the phone rides the Weather tab since it
    # has no tab of its own ("no new tab" per the mobile gate) — the fleet
    # slot must NOT be excluded from the Weather tab the way laundry is.
    assert not re.search(r'body\[data-tab="weather"\] \.fleet-slot[^{]*\{[^}]*display:\s*none',
                         CSS), "the fleet card must stay on the Weather tab"


def test_css_braces_balanced():
    """styles.css must have balanced braces with depth never going negative.
    Browser error recovery hides a stray top-level `}`, but the same scar
    INSIDE the mobile @media block would silently terminate it and disable
    every tab-visibility rule after it — while all the regex guards (which
    match raw text, not parsed CSS) keep passing. Caught for real on the
    2026-08-17 laundry branch: three stray braces from edit splices."""
    depth = 0
    for i, ch in enumerate(CSS):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, f"stray closing brace at offset {i}"
    assert depth == 0, f"unbalanced braces: depth {depth} at EOF"


# --- versioning guards ------------------------------------------------------
# The asset cache-busts are unified to the app version (scripts/release.py is
# the sole writer). These pin that invariant so a stray hand-edit that
# reintroduces a drifting ?v= number fails CI, the way f712ac0 would have.

ROOT = Path(__file__).resolve().parents[1]


def test_version_file_is_clean_semver():
    v = (ROOT / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"VERSION must be clean SemVer, got {v!r}"


def test_every_asset_cache_bust_equals_the_version():
    version = (ROOT / "VERSION").read_text().strip()
    index = (STATIC / "index.html").read_text()
    busts = re.findall(r"\?v=([\w.]+)", index)
    assert busts, "index.html should carry ?v= cache-busts on its assets"
    drifted = sorted({b for b in busts if b != version})
    assert not drifted, (f"asset cache-busts must all equal VERSION ({version}); "
                         f"drifted: {drifted} — run scripts/release.py, don't hand-edit ?v=")


def test_dunder_version_matches_the_version_file():
    import family_hub
    assert family_hub.__version__ == (ROOT / "VERSION").read_text().strip()


def test_dockerfile_ships_version():
    """The running app reads VERSION at the repo root (=/app in the image) for
    /api/version + family_hub.__version__. If the Dockerfile doesn't COPY it,
    the readout silently falls back to '0.0.0+unknown' — fail-soft hiding a real
    break. Guard so the image always carries it."""
    dockerfile = (ROOT / "web.Dockerfile").read_text()
    assert re.search(r"COPY\s+.*\bVERSION\b", dockerfile), \
        "web.Dockerfile must COPY VERSION into the image"


# ---------------------------------------------------------------------------
# On-screen keyboard (osk.js) — the wall's touch keyboard. These guard the
# fixes made after the wall turned out to be an HP touchscreen driven by
# Firefox/Wayland, which reports NO touch to the browser (the panel arrives as
# a mouse). See docs/on-screen-keyboard.md.
# ---------------------------------------------------------------------------

def test_osk_activates_only_in_kiosk_mode():
    """The keyboard is for the keyboard-less WALL only. Phones/laptops already
    have a native keyboard, and showing ours there just stacks under theirs - so
    osk.js must NOT gate on touch (a phone is a touch device too). It activates
    only on the latched ?kiosk=1 flag, and bails otherwise."""
    assert "kiosk=1" in OSK, "osk.js must honor a ?kiosk=1 activation flag"
    assert "localStorage" in OSK and "oskKiosk" in OSK, \
        "the kiosk flag must persist to localStorage so it survives navigation"
    # Kiosk-ONLY: the sole activation gate is `if (!kiosk) return`. A touch check
    # must NOT re-enter the gate, or the board comes back on phones.
    assert re.search(r"if\s*\(\s*!kiosk\s*\)\s*return", OSK), \
        "osk.js must gate on kiosk alone (`if (!kiosk) return`)"
    assert "hasTouch" not in OSK and "maxTouchPoints" not in OSK, \
        "osk.js must not activate on touch — that re-shows the board on phones"
    # ?kiosk=0 is the escape hatch: a phone/laptop that opened the wall's kiosk
    # bookmark must be able to clear the latch and get its native keyboard back.
    assert "kiosk=0" in OSK and "removeItem" in OSK, \
        "osk.js must honor ?kiosk=0 to clear the kiosk latch"
    # A storage-blocked wall would silently have NO keyboard; leave a breadcrumb.
    assert "console.warn" in OSK, \
        "a blocked-storage fallback must warn (the wall's only keyboard is this)"


def test_osk_suppresses_the_os_keyboard():
    """The app keyboard is the ONLY one the wall wants; GNOME's touch keyboard
    otherwise muscles in and mishandles Firefox web inputs (two taps to appear,
    backspace never reaching the field). The served fields are marked readonly +
    inputmode=none so the OS never offers a keyboard, while the app keyboard
    still writes their .value. (Safe because the whole module is kiosk-gated.)"""
    assert "readOnly = true" in OSK, \
        "must set inputs readonly to suppress the OS keyboard"
    assert re.search(r"setAttribute\(\s*['\"]inputmode['\"]\s*,\s*['\"]none['\"]",
                     OSK), "must set inputmode=none as well"
    assert "stampTree(document)" in OSK, \
        "existing served fields must be stamped at load"
    assert "MutationObserver" in OSK, \
        "dynamically-created fields must be stamped too"


def test_osk_has_symbol_and_emoji_layers():
    """The keyboard has a two-page symbol set (?123 / #+=) and a categorized
    emoji picker. Guard the layer plumbing so a refactor can't silently drop
    them."""
    for token in ("SYM1_ROWS", "SYM2_ROWS", "EMOJI_CATS", "osk-emoji-grid"):
        assert token in OSK, f"osk.js lost its {token} layer"
    assert "Layer:" in OSK and "setLayer" in OSK, \
        "the command row's mode keys must route through setLayer"


def test_osk_emoji_picker_is_categorized_with_recents():
    """The emoji layer is a category picker (a tab strip over a per-category
    grid) with a persisted 'recently used' tab. Guard the pieces so none of them
    silently regress to the old flat grid."""
    # A recent category + several fixed categories.
    assert re.search(r"id:\s*'recent'", OSK), "emoji picker needs a 'recent' category"
    cats = re.findall(r"id:\s*'(\w+)',\s*tab:", OSK)
    assert len(cats) >= 6, f"expected several emoji categories, found {cats}"
    # Tabs route through EmojiCat: like the mode keys route through Layer:.
    assert "EmojiCat:" in OSK, "category tabs must carry an EmojiCat: data-key"
    # Recents are persisted and recorded on an emoji tap.
    assert "oskEmojiRecent" in OSK and "pushRecent" in OSK, \
        "recently-used emojis must persist to localStorage"
    assert re.search(r"classList\.contains\('osk-emoji'\)\)\s*pushRecent", OSK), \
        "tapping an emoji must record it in the recents"
    # The dedup/cap/sanitize logic is PURE + unit-tested in common.js; osk.js must
    # route through it (not re-inline a slice that could regress uncovered).
    assert "oskRecentRead" in COMMON and "oskRecentPush" in COMMON, \
        "recents dedup/cap/sanitize logic must live (tested) in common.js"
    assert "oskRecentRead" in OSK and "oskRecentPush" in OSK, \
        "osk.js must use the tested common.js recents helpers, not re-inline them"
    # A stored value that isn't one of our emoji must never surface/insert.
    assert "KNOWN_EMOJI" in OSK and re.search(r"KNOWN_EMOJI\.has", OSK), \
        "recents must be filtered to the known emoji set"
    # The empty-recents state shows a placeholder, not a blank/broken grid.
    assert re.search(r"!list\.length", OSK) and "osk-emoji-empty" in OSK, \
        "an empty recents tab must render the placeholder message"


def test_osk_backspace_is_grapheme_aware_for_emoji():
    """An emoji is 2+ UTF-16 code units; a code-unit backspace would leave a
    broken half-character. oskApplyKey must delete a whole grapheme."""
    assert "oskGraphemeBackLen" in COMMON, \
        "common.js lost the grapheme-aware backspace helper"
    assert "Intl.Segmenter" in COMMON, \
        "grapheme backspace should use Intl.Segmenter (with a surrogate fallback)"
    # the Backspace branch must call the helper, not slice a single code unit
    assert re.search(r"oskGraphemeBackLen\(v,\s*a\)", COMMON), \
        "oskApplyKey Backspace must delete a whole grapheme"


def test_osk_emoji_grid_and_mode_keys_are_styled():
    """Every JS-added OSK class needs a rule or it renders unstyled."""
    for cls in (".osk-keys", ".osk-mode", ".osk-emoji-toggle",
                ".osk-emoji-grid", ".osk-emoji", ".osk-emoji-cats",
                ".osk-emoji-cat", ".osk-emoji-empty", ".osk-cancel"):
        assert cls in CSS, f"{cls} is used by osk.js but not styled"


def test_osk_hide_blurs_a_still_focused_field():
    """After Done/Cancel hides the keyboard, the field is usually still focused
    (the osk keeps focus on it while a key is tapped). If left focused, tapping
    the same box again fires NO focusin, so the keyboard can't be re-summoned by
    tapping it - and a later Done no-ops on a null activeInput. Found on the real
    wall (Firefox) - the fake-DOM harness can't see it. hide() must blur a
    still-focused field so the next tap re-focuses and re-shows."""
    m = re.search(r"function hide\(\) \{(.*?)\n  \}", OSK, re.S)
    assert m, "osk.js must define hide()"
    body = m.group(1)
    assert "document.activeElement === el" in body and ".blur()" in body, \
        "hide() must blur the field when it is still the focused element"


def test_osk_cancel_key_closes_without_saving():
    """A ✕ Cancel key must let you dismiss the keyboard WITHOUT submitting, and
    discard what was typed - distinct from Done (which commits). Guard both the
    key and that its handler clears the field and hides but never submits."""
    assert re.search(r"addKey\(cmd,\s*'Cancel'", OSK), \
        "the command row must include a Cancel key"
    # The Cancel branch clears the active field, hides, and returns BEFORE the
    # Done branch's requestSubmit / [data-submit] click.
    m = re.search(r"if \(key === 'Cancel'\) \{(.*?)\n    \}", OSK, re.S)
    assert m, "onKey must handle the Cancel key"
    body = m.group(1)
    assert "el.value = ''" in body and "hide()" in body, \
        "Cancel must clear the field and hide the keyboard"
    assert "requestSubmit" not in body and "data-submit" not in body, \
        "Cancel must NOT submit — that's what Done is for"


def test_todos_repaint_preserves_focus_and_caret():
    """renderTodosPaint rebuilds the list via innerHTML; without restoring focus
    a background /api/todos refresh destroys the focused add-input and drops the
    keyboard the instant it appears (the wall's 'tap twice' bug). Guard the
    focus/caret restore."""
    para = HUB[HUB.index("function renderTodosPaint"):]
    para = para[:para.index("\n}")]
    assert "activeElement" in para and ".focus()" in para, \
        "renderTodosPaint must re-focus the rebuilt add-input"
    assert "setSelectionRange" in para, \
        "renderTodosPaint must restore the caret position after the rebuild"


def test_month_lane_count_matches_css_row_template():
    """The month renderer stops drawing events past MONTH_MAX_LANES and shows
    "+N more" in the row after; the CSS week grid reserves exactly that many
    lane rows. If one side changes without the other, bars either vanish
    into a row the grid doesn't have or the more-chip lands on a lane."""
    import re
    js = (STATIC / "hub.js").read_text()
    css = (STATIC / "styles.css").read_text()
    m = re.search(r"const MONTH_MAX_LANES = (\d+)", js)
    assert m, "MONTH_MAX_LANES missing from hub.js"
    lanes = int(m.group(1))
    rows = re.findall(r"grid-template-rows:\s*\d+px repeat\((\d+), var\(--mg-lane\)\)", css)
    assert rows, ".mg-week row template missing"
    assert all(int(r) == lanes for r in rows), f"CSS lane rows {rows} != MONTH_MAX_LANES {lanes}"


# ------------------------------------------------------------ seasonal looks
# theme.js's SEASONS registry is the source of truth for look ids; everything a
# look needs to paint lives in styles.css and static/seasons/. These guards make
# "added a look to the registry" fail loudly until every piece exists.

def _look_ids():
    theme = (STATIC / "theme.js").read_text()
    reg = theme[theme.index("var SEASONS = ["):theme.index("var SEASON_PREFS")]
    # every hyphenated id is a look (season ids are bare words, pinned by
    # theme.test.mjs "the season registry is well formed")
    ids = [i for i in re.findall(r'\bid: "([^"]+)"', reg) if "-" in i]
    assert ids, "no look ids found in theme.js SEASONS"
    return ids


# A look owns only the photo, its focal point, the leaf colours and the accent
# (matched to the photo). Everything else (surfaces, ink, borders, the glass
# and the wash) stays the THEME's, so Light, Soft, Blue, Grey and Black each
# keep their own character with a season on ("they should still work with
# the seasons on": an early version gave all three dark themes one charcoal).
_LOOK_TOKENS = ["--accent", "--accent-ink", "--accent-soft", "--sn-scene", "--sn-pos"]
# Each season's moving things are coloured once for the whole season, on its
# own block: fall's leaves are per-look (they were picked out of each photo),
# Halloween's bats, spiders and webs are one palette for all five looks.
_SEASON_SHAPE_TOKENS = {
    "fall-": ["--sn-leaf-1", "--sn-leaf-2", "--sn-leaf-3", "--sn-leaf-4"],
    "halloween-": ["--sn-bat", "--sn-bat-glow", "--sn-spider", "--sn-spider-glow",
                   "--sn-web", "--sn-haze"],
}
_THEME_OWNED = ["--ground", "--surface", "--surface-2", "--edge", "--edge-soft",
                "--ink", "--dim", "--faint", "--shadow", "--glass", "--glass-edge", "--sn-wash"]


def _block_after(selector_start):
    i = CSS.index(selector_start)
    return CSS[CSS.index("{", i) + 1:CSS.index("}", i)]


@pytest.mark.parametrize("look", _look_ids())
def test_every_look_sets_its_photo_and_accent_and_leaves_the_theme_alone(look):
    """Each look sets its photo, focal point, leaf colours and a dark-theme
    accent, plus a light-theme accent block. It must NOT set any theme-owned
    token, or it would flatten the five themes into one. Look selectors are
    (0,4,0) so their accent beats every theme+accent block (max 0,3,0)."""
    eve = f':root[data-look="{look}"][data-theme][data-accent]'
    day = f':root[data-look="{look}"][data-accent]:is([data-theme="light"],[data-theme="soft"])'
    assert eve in CSS and day in CSS, f"{look} needs a dark-theme and a light-theme block"
    body = _block_after(eve)
    for tok in _LOOK_TOKENS:
        assert re.search(rf"{re.escape(tok)}\s*:", body), f"{eve} never sets {tok}"
    prefix = next(pre for pre in _SEASON_SHAPE_TOKENS if look.startswith(pre))
    shapes = _SEASON_SHAPE_TOKENS[prefix]
    if prefix == "fall-":
        for tok in shapes:
            assert re.search(rf"{re.escape(tok)}\s*:", body), f"{eve} never sets {tok}"
    else:
        block = _block_after(f':root[data-look^="{prefix}"]')
        for tok in shapes:
            assert re.search(rf"{re.escape(tok)}\s*:", block), \
                f'the [data-look^="{prefix}"] block never sets {tok}'
    assert f'--sn-scene:url("seasons/{look}.webp")' in body, f"{eve} must paint seasons/{look}.webp"
    for sel in (eve, day):
        blk = _block_after(sel)
        for tok in _THEME_OWNED:
            assert not re.search(rf"(?<![\w-]){re.escape(tok)}\s*:", blk), \
                f"{sel} sets {tok}, which belongs to the theme"
    for tok in ("--accent", "--accent-ink", "--accent-soft"):
        assert re.search(rf"{re.escape(tok)}\s*:", _block_after(day)), f"{day} must set its own {tok}"
    # same specificity, and the dark-theme selector also matches Light and
    # Soft: the light-theme accent wins only by coming later in the file
    assert CSS.index(day) > CSS.index(eve), f"{look}: the light-theme block must follow the dark one"
    assert f'.look-swatch[data-look="{look}"] {{' in CSS or \
        f'.look-swatch[data-look="{look}"],' in CSS or \
        f'.look-swatch[data-look="{look}"]\n' in CSS, f"{look} preview tile has no palette"


def test_every_theme_has_its_own_glass_and_wash():
    """With a season on, each of the five themes keeps its character: its own
    translucent glass and its own wash over the photo. Light's are the bare
    :root defaults; the other four set theirs. At night the dim stops the
    blur, so the glass goes nearly solid (the theme's own surface)."""
    root = re.search(r":root\s*\{([^{}]*)\}", CSS).group(1)
    for tok in ("--glass", "--glass-edge", "--sn-wash"):
        assert re.search(rf"{re.escape(tok)}\s*:", root), f"Light's {tok} belongs on the bare :root"
    seen = {}
    for theme in ("soft", "dark", "grey", "black"):
        m = re.search(rf':root\[data-theme="{theme}"\] \{{([^}}]*--glass:[^}}]*)\}}', CSS)
        assert m, f"the {theme} theme sets no glass"
        for tok in ("--glass", "--glass-edge", "--sn-wash"):
            assert re.search(rf"{re.escape(tok)}\s*:", m.group(1)), f"the {theme} theme never sets {tok}"
        seen[theme] = re.search(r"--glass:\s*([^;]+);", m.group(1)).group(1)
    seen["light"] = re.search(r"--glass:\s*([^;]+);", root).group(1)
    assert len(set(seen.values())) == 5, f"two themes share one glass: {seen}"
    assert re.search(r"\.is-night \{ --glass: color-mix\(in srgb, var\(--surface\) 9\d%", CSS), \
        "at night the glass must go nearly solid (no blur behind the dim)"


def _webp_chunks(data):
    """The chunk ids in a RIFF/WebP file (VP8/VP8L/VP8X/EXIF/XMP /ICCP...)."""
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", "not a WebP file"
    ids, i = [], 12
    while i + 8 <= len(data):
        ids.append(data[i:i + 4])
        size = int.from_bytes(data[i + 4:i + 8], "little")
        i += 8 + size + (size & 1)
    return ids


@pytest.mark.parametrize("look", _look_ids())
def test_every_look_ships_a_light_clean_photo_and_a_mark(look):
    """Each look's photo exists, is sharp enough for the wall, and carries NO
    metadata: EXIF/XMP can hold GPS and camera serials, and this repo is
    public (scripts/prep-season-photo.py strips it). Sharp means 2560px wide:
    a softened, 1920px aspen photo read as blur on the wall ("some of the
    pics look blurry"), so width is pinned and the size cap is generous. A
    look with no mark rule would leave an accent-coloured square by the
    wordmark."""
    photo = STATIC / "seasons" / f"{look}.webp"
    assert photo.is_file(), f"missing seasons/{look}.webp"
    data = photo.read_bytes()
    assert len(data) < 2600 * 1024, f"{photo.name} is {len(data) // 1024} KB; try --quality 76"
    chunks = _webp_chunks(data)
    vp8 = data.index(b"VP8 ") if b"VP8 " in data else -1
    assert vp8 > 0, f"{photo.name} is not a lossy WebP (prep-season-photo.py writes VP8)"
    # VP8 key frame: 3-byte tag + start code 9d 01 2a, then 14-bit width/height
    frame = data[vp8 + 8:]
    assert frame[3:6] == b"\x9d\x01\x2a", f"{photo.name}: unexpected VP8 header"
    width = int.from_bytes(frame[6:8], "little") & 0x3FFF
    assert width >= 2560, f"{photo.name} is {width}px wide; re-run prep-season-photo.py at 2560 (no softening)"
    assert not {b"EXIF", b"XMP "} & set(chunks), f"{photo.name} still carries metadata {chunks}"
    season_mark = rf'\[data-look\^="{re.escape(look.split("-")[0])}-"\] \.season-mark'
    assert re.search(rf'\[data-look="{re.escape(look)}"\] \.season-mark[^{{]*\{{[^}}]*--mark:', CSS) or \
        re.search(season_mark + r'[^{]*\{[^}]*--mark:', CSS), \
        f"{look} has no seasonal mark"


def test_the_scene_and_its_preview_paint_the_look_token():
    """The wall layer and the Settings preview both paint the wash over the
    photo at its focal point, so a preview always shows what the wall will."""
    for sel in (r"body > \.season", r"\.look-swatch"):
        assert re.search(sel + r" \{[^}]*background:\s*(?:var\(--sn-haze[^)]*\)[^;]*?,\s*)?"
                         r"var\(--sn-wash\),\s*var\(--sn-scene\) var\(--sn-pos\) / cover", CSS), \
            f"{sel} must paint var(--sn-wash) over var(--sn-scene) at var(--sn-pos)"


def test_glass_keeps_every_section_readable_and_stays_off_fixed_elements():
    """While a look paints, the cards, their buttons, the section titles and
    the top bar are frosted glass. The rules sit inside :where() so they keep
    the plain .card specificity (a section that paints its own background
    keeps it), and none of the glass targets is a fixed/sticky element (the
    iOS tap-through trap in CLAUDE.md)."""
    for target in (".card", ".expand", ".shead h2", ".topbar"):
        assert re.search(r':where\(:root\[data-look\]:not\(\[data-look="none"\]\)( \.wrap)?\) '
                         + re.escape(target) + r"[^{]*\{[^}]*backdrop-filter", CSS), \
            f"{target} must be glass while a look paints"
    for fixed in (".tabbar", ".overlay", ".theme-pop", ".season"):
        assert not re.search(r":where\([^)]*\)\) " + re.escape(fixed) + r"\b[^{]*\{[^}]*backdrop-filter", CSS)
    # empty check rings drawn in --edge nearly vanished on light glass over a
    # bright photo (caught on Misty Road in Light); they use --faint instead
    assert re.search(r'\.chore-check,\s*:where\(:root\[data-look\]:not\(\[data-look="none"\]\) \.wrap\) '
                     r'\.todo-check \{ border-color: var\(--faint\); \}', CSS)
    # glass makes the top bar a stacking context that traps the gear popover:
    # the bar itself must sit above the glass cards or they cover the popover
    tb = re.search(r':where\(:root\[data-look\]:not\(\[data-look="none"\]\)\) \.topbar \{([^}]*)\}', CSS)
    z = tb and re.search(r"z-index:\s*(\d+)", tb.group(1))
    overlay = re.search(r"\.overlay \{[^}]*z-index:\s*(\d+)", CSS)
    assert z and "position: relative" in tb.group(1) and overlay, \
        "the glass top bar must be lifted (position + z-index) above the glass cards"
    assert 0 < int(z.group(1)) < int(overlay.group(1)), \
        "lifted above the cards, but still under the full-screen overlay"
    # the phone's top row is full: the glass bar's padding must shrink there,
    # or the whole phone page spills sideways (it did, by 31px)
    assert re.search(r'@media \(max-width: 1000px\) \{[^}]*\[data-look\]:not\(\[data-look="none"\]\) '
                     r'\.topbar \{ padding: 6px 6px 6px 10px;', CSS)


def test_season_art_files_exist_and_are_credited():
    """Every url("seasons/...") the stylesheet asks for ships in the repo (a
    missing file paints nothing), and every file in static/seasons/ has a
    CREDITS.md row with its source and licence."""
    seasons = STATIC / "seasons"
    for ref in set(re.findall(r'url\("seasons/([^"]+)"\)', CSS)):
        assert (seasons / ref).is_file(), f"styles.css references missing seasons/{ref}"
    credits = (seasons / "CREDITS.md").read_text()
    for f in seasons.iterdir():
        if f.name == "CREDITS.md":
            continue
        assert f"`{f.name}`" in credits, f"seasons/{f.name} is not in CREDITS.md"

def test_season_scene_sits_behind_and_never_takes_a_tap():
    """The scene is a full-viewport fixed layer: it must be inert
    (pointer-events none), painted UNDER the page (z-index -1 with the body's
    own background stepping aside), and free of backdrop-filter (the iOS
    tap-through trap on fixed elements)."""
    m = re.search(r':root\[data-look\]:not\(\[data-look="none"\]\) body > \.season \{([^}]*)\}', CSS)
    assert m, "missing the wall scene rule"
    rule = m.group(1)
    for decl in ("position: fixed", "z-index: -1", "pointer-events: none"):
        assert decl in rule, f"scene rule must set {decl}"
    assert "backdrop-filter" not in rule
    assert re.search(r':root\[data-look\]:not\(\[data-look="none"\]\) body \{ background: transparent; \}', CSS), \
        "the body must step aside or its background hides the scene"
    hub = (STATIC / "hub.js").read_text()
    assert "insertAdjacentHTML('afterbegin', `<span class=\"season\" aria-hidden=\"true\">${seasonFxHtml('back')}" in hub, \
        "the photo (with the far layer) mounts FIRST in <body> (under everything, a direct child for the night dim)"
    # The leaves are their own layer, LAST in <body>, over the cards: behind
    # the glass they were nearly invisible. It must never take a tap, must stay
    # under the top bar (z 30) and every overlay (z 50+), and is not glass.
    assert "insertAdjacentHTML('beforeend', `<span class=\"season-fx\"" in hub
    fx = re.search(r'body > \.season-fx \{([^}]*)\}', CSS)
    assert fx, "missing the leaf layer rule"
    for decl in ("position: fixed", "pointer-events: none"):
        assert decl in fx.group(1), f"leaf layer must set {decl}"
    z = int(re.search(r"z-index:\s*(\d+)", fx.group(1)).group(1))
    top = int(re.search(r':where\(:root\[data-look\]:not\(\[data-look="none"\]\)\) \.topbar \{[^}]*z-index:\s*(\d+)', CSS).group(1))
    assert 0 < z < top, "leaves drift over the cards but under the top bar and its menu"
    assert "backdrop-filter" not in fx.group(1)


def test_season_motion_stops_for_reduced_motion_and_pauses_at_night():
    """The leaf rules are near the end of the file, so the reduced-motion
    override must come AFTER them or it loses the cascade at equal
    specificity (the main reduced-motion block is too early). Night pauses
    the leaves: nobody needs them falling in a dark kitchen."""
    last_anim = max(m.start() for m in re.finditer(r"animation(?:-name)?:\s*[^;]*\bsn-", CSS))
    blocks = [m for m in re.finditer(r"@media \(prefers-reduced-motion: reduce\) \{", CSS)]
    assert blocks and blocks[-1].start() > last_anim, \
        "a reduced-motion block must follow the last seasonal animation rule"
    tail = CSS[blocks[-1].start():]
    block = tail[:tail.index("\n}")]
    # every selector that STARTS an animation, at its own specificity or more
    for sel in (".sn-leaf.fall", ".sn-leaf b", "body > .season"):
        assert sel in block, f"reduced motion must stop {sel}"
    assert re.search(r"animation:\s*none", block), "the block must actually switch the animations off"
    assert re.search(r"\.sn-leaf\.fall \{ top: var\(--y\); \}", block), \
        "still leaves must rest at their own spots, not stack at the top"
    # the wall's leaves sit OVER the cards: resting still, they would cover the
    # same words forever, so reduced motion removes the wall's leaf layer
    assert re.search(r'body > \.season-fx \{ display: none; \}', block), \
        "reduced motion must hide the wall's leaf layer (still leaves hid text)"
    assert re.search(r"\.is-night \.sn-leaf[^{]*\{[^}]*animation-play-state:\s*paused", CSS)
    # never BLUR a leaf: a big blurred moving layer makes the wall's small GPU
    # re-blur it every frame. A small drop shadow (to lift a gold leaf off a
    # gold photo) is fine.
    assert not re.search(r"\.sn-leaf[^{]*\{[^}]*filter:[^;}]*\bblur\(", CSS)


def test_season_controls_are_wired_in_the_popover_and_config():
    index = (STATIC / "index.html").read_text()
    assert 'data-season-set="off"' in index and 'data-season-set="on"' in index
    assert 'class="season-mark"' in index, "the wordmark's seasonal mark"
    assert 'class="look-accent-note"' in index, "the swatches say why they stepped back"
    assert 'class="theme-pop-sep"' in index, "look settings and screen settings are split"
    # inside the popover's .theme-ctl (the click route is scoped to it), with
    # the look controls, above the divider
    pop = index[index.index('id="theme-pop"'):]
    ctl = pop[pop.index('class="theme-ctl"'):pop.index("data-open-settings")]
    assert 'data-season-set="on"' in ctl, "the Season row must sit inside the popover .theme-ctl"
    assert index.index('data-season-set="on"') < index.index('class="theme-pop-sep"') \
        < index.index('data-layout-set="auto"')
    config = (ROOT / "src" / "family_hub" / "config.py").read_text()
    assert re.search(r'"season":\s*\{"on", "off"\}', config), "config.py must accept theme.season"


def test_every_season_surface_is_hidden_by_default():
    """What every default install sees: no look, so no scene, no mark by the
    wordmark (else an accent-coloured square), no accent note. Each hidden rule
    pairs with the rule that shows it while a look paints."""
    show = ':root[data-look]:not([data-look="none"])'
    for cls in ("season", "season-fx", "season-mark", "look-accent-note"):
        assert re.search(rf"(?m)^\.{re.escape(cls)} \{{ display: none;", CSS), f".{cls} must default hidden"
    assert f"{show} body > .season {{" in CSS
    assert f"{show} .season-mark {{" in CSS
    assert f"{show} .look-accent-note {{ display: block; }}" in CSS


# ---- selector-exact guards (a review's mutation tests showed substring
# guards passing with the real rule weakened or deleted) ----

def _rules():
    """(selector_list, body, start) for every flat rule in styles.css."""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        sels = [s.strip() for s in re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S).split(",")]
        out.append(([s for s in sels if s], m.group(2), m.start()))
    return out


def _last_reduced_motion_selectors():
    start = [m.start() for m in re.finditer(r"@media \(prefers-reduced-motion: reduce\) \{", CSS)][-1]
    tail = CSS[start:]
    block = tail[tail.index("{") + 1:tail.index("\n}")]
    stopped, hidden = set(), set()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        sels = {s.strip() for s in m.group(1).split(",")}
        if re.search(r"animation:\s*none", m.group(2)):
            stopped |= sels
        if re.search(r"display:\s*none", m.group(2)):
            hidden |= sels
    return start, stopped, hidden


def test_reduced_motion_stops_every_seasonal_animation_by_its_exact_selector():
    """Every rule that starts a seasonal animation must be matched, selector
    for selector, in the last reduced-motion block (the same selector later in
    the file always wins; a weaker look-alike does not). And the wall's near
    leaves are hidden there by the exact selector that shows them."""
    start, stopped, hidden = _last_reduced_motion_selectors()
    animated = [s for sels, body, pos in _rules() if pos < start
                and re.search(r"animation:\s*[^;]*\bsn-", body) for s in sels]
    assert animated, "found no seasonal animations to check"
    missing = [s for s in animated if s not in stopped]
    assert not missing, f"reduced motion does not stop: {missing}"
    show = ':root[data-look]:not([data-look="none"]) body > .season-fx'
    assert any(show in s for sels, body, _ in _rules() for s in sels), "the near-leaf show rule moved"
    assert show in hidden, "reduced motion must hide the near leaves with the SAME selector that shows them"


def test_night_hides_the_near_leaves_and_pauses_the_far_ones():
    """At night the dim makes .wrap its own stacking layer, so the near leaves
    (z 20) would paint over the top bar and the gear menu: they are hidden.
    The far leaves pause; both the fall and the sway must stop. Night glass
    goes solid on <body> (where hub.js puts is-night)."""
    assert re.search(r'(?m)^:root\[data-look\]:not\(\[data-look="none"\]\) body\.is-night > \.season-fx \{ display: none; \}', CSS)
    # every moving thing the far layer keeps at night must be named in a
    # paused rule. The selectors are collected rather than matched as one
    # exact line, so re-wrapping that declaration is not a false failure.
    paused = set()
    for sels, body, _ in _rules():
        if "animation-play-state: paused" in body:
            paused |= {sel.strip() for sel in sels}
    for sel in (".is-night .sn-leaf", ".is-night .sn-leaf b", ".is-night .sn-bat",
                ".is-night .sn-bat i", ".is-night .sn-bat b", ".is-night .sn-dangle i"):
        assert sel in paused, f"night must pause {sel}: a hidden animation still ticks"
    assert re.search(r"(?m)^\.is-night \{ --glass: color-mix\(in srgb, var\(--surface\) 9\d%", CSS)


def test_no_look_rule_anywhere_sets_a_theme_owned_token():
    """Not just the two palette blocks: ANY rule that names a look id must
    leave the theme's tokens alone, or it flattens the five themes."""
    for look in _look_ids():
        for sels, body, _ in _rules():
            if not any(f'data-look="{look}"' in s for s in sels):
                continue
            for tok in _THEME_OWNED:
                assert not re.search(rf"(?<![\w-]){re.escape(tok)}\s*:", body), \
                    f"a {look} rule ({sels[0]}) sets the theme-owned {tok}"


def test_leaves_fall_at_two_depths():
    """Both leaf layers are shown, the far set has its own six lanes (not the
    near leaves' paths), and the far set carries no shadow."""
    shown = {s for sels, body, _ in _rules() if "display: block" in body for s in sels}
    for sel in (':root[data-look^="fall-"] body > .season .sn-leaves',
                ':root[data-look^="fall-"] body > .season-fx .sn-leaves'):
        assert sel in shown, f"{sel} is never shown"
    near = dict(re.findall(r"(?m)^\.sn-leaf:nth-child\((\d)\) \{ --x: ([\d.]+%)", CSS))
    far = dict(re.findall(r"(?m)^\.sn-leaves\.back \.sn-leaf:nth-child\((\d)\) \{ --x: ([\d.]+%)", CSS))
    assert sorted(near) == sorted(far) == [str(i) for i in range(1, 7)], "six near and six far leaves"
    assert all(near[i] != far[i] for i in near), "far leaves need their own lanes, or the depth is lost"
    assert re.search(r"\.sn-leaves\.back \.sn-leaf\.fall \{ filter: none; \}", CSS)


def test_phone_top_row_and_leaf_layer_fit_the_phone():
    """The phone's top row only just fits: a two-digit hour overflowed it by
    8px. Each fix is pinned, scoped so a forced-Desktop TV keeps the wall
    layout; and the near leaves stop above the tab bar plus the iPhone
    home-indicator inset."""
    m = re.search(r"@media \(max-width: 1000px\) \{((?:[^{}]|\{[^{}]*\})*)\}", CSS[CSS.index("seasonal looks (fall)"):])
    assert m, "no phone block in the seasonal section"
    phone = m.group(1)
    scope = ':root:not([data-layout="desktop"])[data-look]:not([data-look="none"])'
    assert f"{scope} .season-mark {{ display: none; }}" in phone
    assert re.search(re.escape(f"{scope} .wordmark {{") + r"[^}]*min-width: 0;[^}]*text-overflow: ellipsis", phone)
    assert f"{scope} .topbar {{ padding: 6px 6px 6px 10px;" in phone
    assert re.search(r'body > \.season-fx \{ bottom: calc\(64px \+ env\(safe-area-inset-bottom, 0px\)\); \}', CSS)


def _png_size(path):
    """(width, height) from a PNG's IHDR, no image library needed."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    assert data[12:16] == b"IHDR", f"{path.name}: no IHDR"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def test_halloween_sprite_sheets_match_the_frames_the_css_steps_through():
    """The bat's wingbeat and the spider's walk are real drawn frames in one
    row (see seasons/CREDITS.md). Each is a strip as wide as its frame count,
    sliding behind a one-frame window. Three numbers have to agree or the
    animation tears: the strip's width, the frame count in steps(), and the
    cells actually in the file — and the cell's shape must match the window
    it slides behind, or every frame is stretched.

    The strip is moved with transform, never with the mask's own position:
    mask-position is not a compositable property, so animating it repaints
    seven shadowed bats on the main thread every frame, all day, on a wall
    driven by an i3's integrated graphics."""
    for sheet, window in (("bat-flap.png", (1.0, 0.83)), ("spider-walk.png", (58.0, 76.0))):
        strip = re.search(rf'::before \{{[^}}]*width: (\d+)%;[^}}]*mask: url\("seasons/{sheet}"\) 0 0 / 100% 100%', CSS)
        assert strip, f"seasons/{sheet} must be painted on a ::before strip, stepped by transform"
        frames = int(strip.group(1)) // 100
        haunt = CSS[CSS.index("Halloween's creatures"):]
        steps = {int(m) for m in re.findall(r"steps\((\d+)[,)]", haunt)}
        assert frames in steps, f"{sheet}: the strip holds {frames} frames, but no steps({frames}) drives it"
        w, h = _png_size(STATIC / "seasons" / sheet)
        assert w % frames == 0, f"{sheet} is {w}px wide, not a whole number of {frames} frames"
        cell = (w / frames) / h
        assert abs(cell - window[0] / window[1]) < 0.12, \
            f"{sheet}'s frames are {cell:.2f} wide per tall; its window is {window[0] / window[1]:.2f}, so frames stretch"
    for name in ("sn-bat-flap", "sn-walk"):
        kf = re.search(rf"@keyframes {name} \{{([^}}]*)\}}", CSS)
        assert kf, f"no @keyframes {name}"
        assert "transform: translate3d(" in kf.group(1) and "mask-position" not in kf.group(1), \
            f"{name} must step the strip with transform (mask-position repaints every frame)"


def test_halloween_creatures_are_sized_off_one_scale_so_a_phone_can_shrink_them():
    """Wall-sized bats, spiders and webs swamped a 390px phone (one web
    covered the whole first card). Every creature is sized through --sn-k, and
    the phone block sets it below 1, scoped so a forced-Desktop TV keeps the
    wall's sizes."""
    # each creature's OWN rule (anchored at a line start): an unanchored
    # search took the first rule that merely ended in ".sn-bat {"
    for sel in (r"(?m)^\.sn-bat \{", r"(?m)^\.sn-crawl \{", r"(?m)^\.sn-dangle \{", r"(?m)^\.sn-web \{"):
        m = re.search(sel + r"([^}]*)\}", CSS)
        assert m and "var(--sn-k, 1)" in m.group(1), f"{sel} must size itself through --sn-k"
    phone = re.search(r'@media \(max-width: 1000px\) \{[^{}]*:root:not\(\[data-layout="desktop"\]\)'
                      r'\[data-look\^="halloween-"\] \{ --sn-k: (0?\.\d+); \}', CSS)
    assert phone, "the phone block must shrink the creatures with --sn-k"
    assert 0 < float(phone.group(1)) < 1


def test_the_crawling_spider_is_parked_until_hub_js_walks_it():
    """hub.js walks the crawler (CSS could not keep its legs in step with its
    body). Two things must hold whatever hub.js does: it starts off-screen, so
    a browser without element.animate never shows a spider frozen in the
    corner, and its legs only cycle while it carries the .walking class."""
    m = re.search(r"\.sn-crawl \{([^}]*)\}", CSS)
    assert m and re.search(r"transform: translate3d\(-\d+px, -\d+px, 0\)", m.group(1)), \
        "the crawler must be parked off-screen by default"
    legs = re.search(r"\.sn-crawl\.walking b::before, \.sn-dangle\.walking b::before \{([^}]*)\}", CSS)
    assert legs and "animation: sn-walk" in legs.group(1), \
        "the walk cycle must hang off .walking, or the legs walk on the spot"
    hub = (STATIC / "hub.js").read_text()
    for fn in ("function spiderWalk()", "function spiderDrop()"):
        assert fn in hub, f"hub.js must define {fn}"
    assert "spiderWalk();" in hub and "spiderDrop();" in hub, "both spiders must be started"
    # they must stand down when the layer is hidden: no look, night, reduced
    # motion, or a background tab
    motion = hub[hub.index("function snMotion("):hub.index("async function spiderWalk()")]
    assert "typeof el.animate !== 'function'" in motion, "must bail without the Web Animations API"
    # what "should it be moving?" means, not how deep the markup happens to
    # be: an earlier version walked two fixed levels up the tree and read
    # display there, which silently inverted the moment the markup gained a
    # level (a spider would then walk over a night-dimmed wall)
    assert "document.hidden" in motion, "a background tab must stand them down"
    assert 'getAttribute(\'data-look\')' in motion, "no look painted must stand them down"
    assert "checkVisibility" in motion, "visibility must be asked of the element, not inferred from nesting"
    # one move must never leave a spider frozen with its legs cycling: the
    # animation is raced against a deadline and the legs stop in a finally
    go = motion[motion.index("async go("):]
    assert "try {" in go and "finally {" in go, "a move must clean up after itself whatever happens"
    assert "Promise.race" in go, "a timeline that stops advancing must not hang the loop for good"
    walk = hub[hub.index("async function spiderWalk()"):hub.index("async function spiderDrop()")]
    assert walk.count("m.still();") >= 1 and "finally {" in walk, \
        "the legs stop in a finally, or a failed move leaves them cycling under a still body"


def test_the_creature_layer_belongs_to_halloween_alone():
    """The webs, bats and spiders show for a halloween-* look and for nothing
    else. The layer that carries them is now shown for EVERY season (the
    leaves' old fall-only rule was widened), so this prefix is the only thing
    keeping bats off the fall photos."""
    shown = {s for sels, body, _ in _rules() if "display: block" in body for s in sels}
    for sel in (':root[data-look^="halloween-"] body > .season .sn-haunt',
                ':root[data-look^="halloween-"] body > .season-fx .sn-haunt',
                '.look-swatch[data-look^="halloween-"] .sn-haunt'):
        assert sel in shown, f"{sel} is never shown"
    for sels, body, _ in _rules():
        if "display: block" not in body:
            continue
        for sel in sels:
            if ".sn-haunt" in sel:
                assert 'halloween-' in sel, f"{sel} would show the creatures outside Halloween"


def test_the_back_layer_order_matches_the_rules_that_place_it():
    """The far layer is addressed by position: webs first, then bats. hub.js
    emits them in that order and styles.css numbers them to match; swapping
    either leaves the webs with no size and the bats with no lane."""
    hub = (STATIC / "hub.js").read_text()
    back = hub[hub.index("if (depth === 'back')"):hub.index("return '<span class=\"sn-haunt front\">")]
    order = re.findall(r"sn-web|bat \+ bat|\bbat\b", back)
    assert back.count('class="sn-web"') == 2, "two webs open the back layer"
    assert back.index('class="sn-web"') < back.index("bat"), "the webs come first, as the rules assume"
    webs = {int(m) for m in re.findall(r"\.sn-web:nth-child\((\d)\)", CSS)}
    bats = {int(m) for m in re.findall(r"\.sn-haunt\.back \.sn-bat:nth-child\((\d)\)", CSS)}
    assert webs == {1, 2}, f"the webs are children 1 and 2, not {sorted(webs)}"
    assert bats == {3, 4, 5}, f"the far bats follow the webs, not {sorted(bats)}"
    assert back.count(" + bat") == 3, "three far bats, one per rule"
    assert order  # the markup was read, not an empty slice


def test_every_creature_token_a_season_declares_is_actually_used():
    """A token declared and never referenced paints nothing: a typo in the
    reference (var(--sn-bat-nope)) leaves the bats invisible and every other
    guard still green."""
    for prefix, tokens in _SEASON_SHAPE_TOKENS.items():
        if prefix == "fall-":
            continue
        for tok in tokens:
            assert re.search(rf"var\({re.escape(tok)}[,)]", CSS), \
                f"{tok} is declared for {prefix} but nothing paints with it"


def test_the_dangling_spider_hangs_from_a_visible_thread():
    """The thread is the whole illusion: a hairline in the web colour running
    from the spider up past the top of the screen, INSIDE the box that sways,
    so thread and spider lean together (they did not, at first)."""
    thread = re.search(r"\.sn-dangle i::before \{([^}]*)\}", CSS)
    assert thread, "the dangling spider has no thread"
    body = thread.group(1)
    assert "width: 1px" in body, "the thread is a hairline"
    assert "height: 100vh" in body, "it must reach past the top of the screen at any drop"
    assert "var(--sn-web)" in body, "it is drawn in the look's own web colour"
    sway = re.search(r"\.sn-dangle\.visiting i \{([^}]*)\}", CSS)
    assert sway and "sn-dangle-sway" in sway.group(1), \
        "the sway belongs to the box that holds BOTH the thread and the spider"


def test_reduced_motion_takes_the_far_bats_but_keeps_the_webs():
    """Still bats read as dropped at the left edge, so they go. The webs
    never moved, so they stay: a Halloween look with no webs at all is not
    what reduced motion is for."""
    _, _, hidden = _last_reduced_motion_selectors()
    assert ':root[data-look]:not([data-look="none"]) body > .season .sn-bat' in hidden, \
        "reduced motion must hide the far bats"
    assert not any(".sn-web" in sel for sel in hidden), \
        "the webs do not move; reduced motion must keep them"


def test_the_preview_tile_paints_halloween_and_holds_still():
    """A Settings tile is a live preview at 128px. It needs the season's own
    creature colours, everything still, and its own sizes — and no wall rule
    may reach inside it (the phone's web offsets did, and pushed both webs
    off the tile)."""
    swatch = _block_after('.look-swatch[data-look^="halloween-"]')
    for tok in _SEASON_SHAPE_TOKENS["halloween-"]:
        assert re.search(rf"{re.escape(tok)}\s*:", swatch), f"a preview tile never sets {tok}"
    still = re.search(r"\.look-swatch \.sn-bat \{([^}]*)\}", CSS)
    assert still and "animation: none" in still.group(1), "preview bats must hold still in the tile"
    assert re.search(r"\.look-swatch \{[^}]*--sn-k: 1;", CSS), \
        "a tile keeps wall scale: --sn-k is the phone's shrink for the wall, not for a 128px tile"
    # every rule that places a web is either the tile's own or scoped to the
    # wall layer, so the two can never fight
    for sels, body, _ in _rules():
        if not re.search(r"(left|right|top|bottom):", body):
            continue
        for sel in sels:
            if ".sn-web" not in sel:
                continue
            assert ".look-swatch" in sel or "body > .season" in sel, \
                f"{sel} places a web without saying whether it means the wall or a tile"


def test_night_takes_the_far_bats():
    """Paused at night, a third of the far bats stopped mid-sky with their
    wingbeat (on b::before, which the pause never reached) still going: a
    bat hanging in the air flapping on the spot. They go at night."""
    assert re.search(r'(?m)^:root\[data-look\]:not\(\[data-look="none"\]\) body\.is-night > \.season \.sn-bat \{ display: none; \}', CSS)


def test_leaves_rock_and_drift_as_they_fall():
    """A leaf falling in a dead-straight lane, flat to the glass, read as a
    shape on a conveyor. Each one drifts sideways on its own --drift and
    rocks in 3D on the `rotate` property (so it stacks with the sway's
    transform instead of replacing it), and the rock stays short of edge-on
    so a leaf never blinks out."""
    fall = re.search(r"@keyframes sn-fall \{[^\n]*\}", CSS).group(0)
    assert "var(--drift" in fall, "the fall must carry the leaf's sideways drift"
    rock = re.search(r"@keyframes sn-rock \{([^\n]*)\}", CSS)
    assert rock and "rotate:" in rock.group(1) and "transform" not in rock.group(1), \
        "the rock must animate `rotate`, or it would replace the sway"
    for deg in re.findall(r"(-?\d+)deg", rock.group(1)):
        assert abs(int(deg)) < 80, "a rock near 90deg turns the leaf edge-on and it vanishes"
    anim = re.search(r"\.sn-leaf\.fall b \{ animation:([^}]*)\}", CSS).group(1)
    assert "sn-sway" in anim and "sn-rock" in anim
    drifts = re.findall(r"(?m)^\.(?:sn-leaves\.back \.)?sn-leaf:nth-child\(\d\) \{[^}]*--drift: (-?\d+)px", CSS)
    assert len(drifts) == 12 and len(set(drifts)) > 6, "every leaf gets its own drift"


def test_the_dangling_spider_hangs_head_down():
    """A spider on silk hangs from the tip of its abdomen, head down. The
    drawing faces up, so the dangler turns over, and the thread ends near
    the top of its box, where the abdomen now is (not in its middle)."""
    assert re.search(r"(?m)^\.sn-dangle b \{ transform: rotate\(180deg\); \}", CSS)
    thread = re.search(r"\.sn-dangle i::before \{([^}]*)\}", CSS).group(1)
    assert int(re.search(r"bottom: (\d+)%", thread).group(1)) >= 80


def test_no_wingbeat_steps_faster_than_the_screen_draws():
    """A bat once stepped its 15 drawn frames in 0.19s: 79 frames a second,
    faster than the screen draws, so frames dropped and the wingbeat
    stuttered. Every bat's --flap keeps the strip at or under 60 a second."""
    frames = int(re.search(r"animation: sn-bat-flap [^;]*steps\((\d+)", CSS).group(1))
    flaps = re.findall(r"--flap: (\d*\.?\d+)s", CSS)
    assert flaps, "no bat sets its wingbeat"
    for v in flaps:
        assert frames / float(v) <= 60, f"--flap {v}s steps the wingbeat faster than the screen draws"


def test_the_leaf_rock_has_depth():
    """Without perspective on the leaf, the 3D rock flattens into a plain
    squash and the leaf stops reading as tipping in the air."""
    leaf = re.search(r"(?m)^\.sn-leaf \{([^}]*)\}", CSS)
    assert leaf and "perspective:" in leaf.group(1)
def test_todo_done_grace_matches_the_server_window():
    # The wall schedules its own refresh for the moment a checked item is
    # archived; if the two numbers drift, the row either vanishes early (the
    # server still returns it) or lingers for a whole extra poll.
    import re
    from family_hub import todos
    m = re.search(r"const TODO_DONE_GRACE_MS = (\d+) \* 60000;", ALL_JS)
    assert m, "TODO_DONE_GRACE_MS not found in hub.js"
    assert int(m.group(1)) == todos.DONE_GRACE_MIN


def test_laundry_waiting_load_lies_still():
    # the wet load in a waiting washer must not slosh: both longhands, same
    # trap as the done rule (.ln-washer .ln-heap re-sets animation-name)
    assert re.search(r"\.ln-ph-waiting \.ln-heap\s*\{[^}]*animation:\s*none;[^}]*"
                     r"animation-play-state:\s*paused", CSS) or re.search(
        r"\.ln-ph-done \.ln-heap, \.ln-ph-waiting \.ln-heap\s*\{\s*animation:\s*none;"
        r"\s*animation-play-state:\s*paused", CSS), \
        "the waiting heap must be stilled like the done heap"
    for rule in (r"\.ln-ph-waiting \.ln-arc\s*\{[^}]*var\(--warn\)",
                 r"\.ln-ph-waiting \.ln-big\s*\{[^}]*var\(--warn\)"):
        assert re.search(rule, CSS), rule


def test_overlays_and_modals_are_announced_as_modal_dialogs():
    """Screen readers and keyboard users need to know a full-screen overlay or
    a modal took over the page. Each layer is a dialog (the delete confirm an
    alertdialog), marked modal, named, and focusable as a fallback target when
    it has no button of its own to land focus on."""
    index = (STATIC / "index.html").read_text()
    for el_id, role in (("overlay", "dialog"), ("ev-modal", "dialog"),
                        ("chore-modal", "dialog"), ("confirm-modal", "alertdialog")):
        m = re.search(r'<div [^>]*id="%s"[^>]*>' % el_id, index)
        assert m, f"#{el_id} missing from index.html"
        tag = m.group(0)
        assert f'role="{role}"' in tag, f"#{el_id} must be role={role}"
        assert 'aria-modal="true"' in tag, f"#{el_id} must be aria-modal"
        assert "aria-label=" in tag or "aria-labelledby=" in tag, f"#{el_id} needs a name"
        assert 'tabindex="-1"' in tag, f"#{el_id} needs tabindex=-1 as a focus fallback"


def test_long_titles_wrap_instead_of_overflowing():
    """review 2026-09-22: a very long single word (a pasted URL) ran past the
    edge of agenda and chore rows. Flex children need min-width: 0 to shrink
    below their longest word, and overflow-wrap: anywhere to break it."""
    for sel in (".cal-title", ".chore-title", ".padmin-name"):
        rule = re.search(r"(?m)^" + re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
        assert rule, sel
        assert "min-width: 0" in rule.group(1) and "overflow-wrap: anywhere" in rule.group(1), sel
    ev = re.search(r"(?m)^\.ev-title\s*\{([^}]*)\}", CSS)
    assert ev and "overflow-wrap: anywhere" in ev.group(1)

