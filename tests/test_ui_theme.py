"""The web UI ships one palette, light only.

There is no dark media query, no data-theme toggle and no per-page dark surface anywhere in the
static files or templates; the browser is told the page is light (color-scheme) so form controls
and scrollbars follow; every colour outside the :root token block is a token; and the token
pairs that actually meet on screen (text on page, muted on panel, accent on its wash, white on
the primary button, ...) hold WCAG AA, computed here from app.css so a future edit cannot
regress them silently. Finally every page is rendered through the TestClient and checked for
leftover dark literals.
"""
import re
from pathlib import Path

import pytest

from salescoach import web as web_pkg
from test_p4_support import make_email
from test_web import FakeGmail, FakeLive, app, client, gmail, live, processed  # noqa: F401

STATIC = Path(web_pkg.__file__).parent / "static"
TEMPLATES = Path(web_pkg.__file__).parent / "templates"
CSS_FILES = sorted(STATIC.glob("*.css"))
JS_FILES = sorted(STATIC.glob("*.js"))
HTML_FILES = sorted(TEMPLATES.glob("*.html"))

# Anything that would only make sense on a dark surface, or that switches surfaces at all.
DARK_HOOKS = ["prefers-color-scheme", "data-theme", "color-scheme: dark", 'content="dark"', "surface-ink"]
DARK_LITERALS = ["#16120d", "#211c14", "rgba(245, 239, 225", "#f5efe1", "#ece4d2", "#ff5735", "#ff6e50",
                 "#5cc98b", "#aba089", "#6b6353", "#d8ceb7", "#ea3b1a", "rgba(0, 0, 0, .28"]

# Body text 4.5:1; UI boundaries and large text 3:1 (WCAG 2.1 AA, 1.4.3 and 1.4.11).
AA_TEXT, AA_UI = 4.5, 3.0


# ---- colour maths ---------------------------------------------------------------------------

def _lin(c: int) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb) -> float:
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def parse_colour(value: str):
    """'#RRGGBB' -> (rgb, 1.0); 'rgba(r, g, b, a)' -> (rgb, a)."""
    value = value.strip()
    if value.startswith("#") and len(value) == 7:
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)), 1.0
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)", value)
    if m:
        return tuple(int(m.group(i)) for i in (1, 2, 3)), float(m.group(4) or 1)
    raise ValueError(f"not a colour: {value!r}")


def over(fg, alpha, bg):
    """The solid colour a translucent fill shows as on an opaque background."""
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


def root_tokens() -> dict:
    css = (STATIC / "app.css").read_text()
    m = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    assert m, "app.css has no :root block"
    out = {}
    for line in m.group(1).splitlines():
        line = line.split("/*", 1)[0].strip()
        if line.startswith("--") and ":" in line:
            name, value = line.split(":", 1)
            out[name.strip()] = value.strip().rstrip(";").strip()
    return out


def solid(tokens: dict, name: str, on=None):
    rgb, a = parse_colour(tokens[name])
    if a < 1:
        assert on is not None, f"{name} is translucent; it needs a backing colour"
        return over(rgb, a, on)
    return rgb


def token_pairs(tokens: dict):
    """(label, foreground, background, minimum ratio) for every pair the stylesheets put together."""
    bg, panel = solid(tokens, "--bg"), solid(tokens, "--panel")
    pairs = []
    for surface_name, surface in (("bg", bg), ("panel", panel)):
        wash = solid(tokens, "--accent-wash", on=surface)
        good_wash = solid(tokens, "--good-wash", on=surface)
        pairs += [
            (f"text on {surface_name}", solid(tokens, "--text"), surface, AA_TEXT),
            (f"muted on {surface_name}", solid(tokens, "--muted"), surface, AA_TEXT),
            (f"accent on {surface_name}", solid(tokens, "--accent"), surface, AA_TEXT),
            (f"accent-hover on {surface_name}", solid(tokens, "--accent-hover"), surface, AA_TEXT),
            (f"good on {surface_name}", solid(tokens, "--good"), surface, AA_TEXT),
            (f"warn on {surface_name}", solid(tokens, "--warn"), surface, AA_TEXT),
            (f"accent on accent-wash over {surface_name}", solid(tokens, "--accent"), wash, AA_TEXT),
            (f"text on accent-wash over {surface_name}", solid(tokens, "--text"), wash, AA_TEXT),
            (f"good on good-wash over {surface_name}", solid(tokens, "--good"), good_wash, AA_TEXT),
            (f"line-strong on {surface_name}", solid(tokens, "--line-strong"), surface, AA_UI),
            (f"grey on {surface_name}", solid(tokens, "--grey"), surface, AA_UI),
            (f"accent-line on {surface_name}", solid(tokens, "--accent-line", on=surface), surface, AA_UI),
        ]
    pairs += [
        ("on-accent on accent", solid(tokens, "--on-accent"), solid(tokens, "--accent"), AA_TEXT),
        ("on-accent on accent-hover", solid(tokens, "--on-accent"), solid(tokens, "--accent-hover"), AA_TEXT),
        ("on-accent on good", solid(tokens, "--on-accent"), solid(tokens, "--good"), AA_TEXT),
        ("text on field", solid(tokens, "--text"), solid(tokens, "--field"), AA_TEXT),
        ("muted on field (placeholder)", solid(tokens, "--muted"), solid(tokens, "--field"), AA_TEXT),
        ("line-strong on field", solid(tokens, "--line-strong"), solid(tokens, "--field"), AA_UI),
    ]
    return pairs


# ---- static files -----------------------------------------------------------------------------

def _has(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


@pytest.mark.parametrize("path", CSS_FILES + JS_FILES + HTML_FILES, ids=lambda p: p.name)
def test_no_dark_mode_anywhere(path):
    text = path.read_text()
    for hook in DARK_HOOKS:
        assert not _has(text, hook), f"{path.name} still carries {hook!r}"
    for literal in DARK_LITERALS:
        assert not _has(text, literal), f"{path.name} still carries the dark literal {literal!r}"


def test_the_page_declares_itself_light():
    tokens_block = re.search(r":root\s*\{(.*?)\n\}", (STATIC / "app.css").read_text(), re.S).group(1)
    assert "color-scheme: light" in tokens_block
    assert '<meta name="color-scheme" content="light">' in (TEMPLATES / "base.html").read_text()
    # base.html has no theme toggling of any kind.
    base = (TEMPLATES / "base.html").read_text()
    assert "theme" not in base.lower()
    # exactly one color-scheme declaration across the stylesheets: the light one on :root
    declared = [m.group(0) for css in CSS_FILES for m in re.finditer(r"color-scheme\s*:\s*\w+", css.read_text())]
    assert declared == ["color-scheme: light"]


@pytest.mark.parametrize("path", CSS_FILES, ids=lambda p: p.name)
def test_colours_outside_root_are_tokens(path):
    css = path.read_text()
    if path.name == "app.css":
        css = re.sub(r":root\s*\{.*?\n\}", "", css, count=1, flags=re.S)
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|color-mix\(", css)
    assert literals == [], f"{path.name} hardcodes colours outside the token block: {literals}"
    assert "@media (prefers" not in css


def test_root_has_exactly_one_palette():
    css = (STATIC / "app.css").read_text()
    assert len(re.findall(r":root\s*\{", css)) == 1
    assert "body.surface" not in css
    tokens = root_tokens()
    for name in ("--bg", "--panel", "--field", "--text", "--muted", "--line", "--line-strong", "--grey", "--accent",
                 "--accent-hover", "--accent-wash", "--accent-line", "--on-accent", "--good", "--good-wash",
                 "--warn"):
        assert name in tokens, name
    # a light theme: the page is white, panels barely off-white, text near black
    assert luminance(solid(tokens, "--bg")) > 0.95
    assert luminance(solid(tokens, "--panel")) > 0.85
    assert luminance(solid(tokens, "--field")) > 0.95
    assert luminance(solid(tokens, "--text")) < 0.02


def test_token_pairs_meet_wcag_aa():
    tokens = root_tokens()
    failures = []
    for label, fg, bg, need in token_pairs(tokens):
        ratio = contrast(fg, bg)
        if ratio < need:
            failures.append(f"{label}: {ratio:.2f}:1 < {need}:1")
    assert not failures, "\n".join(failures)


# ---- rendered pages ---------------------------------------------------------------------------

def _assert_light(url: str, html: str):
    for needle in DARK_HOOKS + DARK_LITERALS:
        assert not _has(html, needle), (url, needle)


def test_every_page_renders_light(client, processed, db):  # noqa: F811
    call, deal = processed["call"], processed["deal"]
    run_id = db.execute("SELECT id FROM agent_runs WHERE call_id=? AND agent='actions'", (call,)).fetchone()[0]
    nudge = make_email(db, deal)
    urls = [
        "/", f"/calls/{call}", f"/calls/{call}/runs", f"/runs/{run_id}", "/loops", "/loops?status=all", "/deals",
        f"/deals/{deal}", f"/deals/{deal}/intel", f"/deals/{deal}/prep", f"/deals/{deal}/outcome", "/coach",
        "/coach/intel", f"/coach/live/{call}", f"/live/{call}", "/import", "/followups", "/replies",
        f"/nudges/{nudge}", "/calendar", "/learning", "/setup", "/setup/you", "/setup/method",
        "/setup/method/custom", "/setup/model", "/setup/sources", "/setup/connections", "/setup/review",
    ]
    for url in urls:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        if "<html" in r.text:      # full pages; the intel and outcome routes answer with fragments
            assert '<meta name="color-scheme" content="light">' in r.text, url
            assert "<body" in r.text
        _assert_light(url, r.text)
    for css in CSS_FILES:
        r = client.get(f"/static/{css.name}")
        assert r.status_code == 200, css.name
        _assert_light(css.name, r.text)
