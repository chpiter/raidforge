"""Monochrome guard, static half: the palette must be black and white only.

Why a test for this
-------------------
The appliance renders on the bare Linux console, which has 16 ANSI colours.  A
"dark brand colour" such as ``#002B49`` is quantised to ``SGR 90`` (bright
black) there, and several VGA/fbcon implementations draw that as *nothing* on a
coloured bar - the disk table header turned into a blank cyan stripe while every
headless test on a 24-bit terminal stayed green.

These assertions are cheap and catch the leak at review time instead of at the
customer site.  The live half (``test_mono_live.py``) checks the bytes that
actually reach the terminal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from raidforge.config import load_config
from raidforge.ui import theme as theme_mod

APP_DIR = Path(__file__).resolve().parent.parent / "app" / "raidforge"
ALLOWED_HEX = {"#000000", "#FFFFFF"}

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")
# Rich markup that means a colour: [red], [/b red], [#ff0000], [on blue]...
COLOUR_TAG_RE = re.compile(
    r"\[/?(?:[a-z]+\s+)*(red|green|blue|cyan|magenta|yellow|orange|purple|white|black"
    r"|#[0-9a-fA-F]{3,6}|on\s+[a-z]+)\]",
    re.IGNORECASE,
)


CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _colour_literals(text: str) -> set:
    """Hex colours in actual declarations, ignoring prose in comments."""

    without_comments = CSS_COMMENT_RE.sub("", text)
    return {value.upper() for value in HEX_RE.findall(without_comments)}


# ---------------------------------------------------------------------------
# the stylesheet
# ---------------------------------------------------------------------------
def test_css_uses_only_black_and_white():
    css = theme_mod.build_css(load_config())
    literals = _colour_literals(css)
    assert literals <= ALLOWED_HEX, f"non-monochrome colours in the stylesheet: {literals - ALLOWED_HEX}"


def test_css_references_no_brand_variables():
    css = theme_mod.build_css(load_config())
    for banned in ("$accent", "$danger", "$ok", "$cursor"):
        assert banned not in css, f"{banned} would reintroduce a third colour"


def test_theme_variables_are_monochrome():
    theme = theme_mod.mono_theme()
    colours = {}
    for name in ("primary", "secondary", "accent", "warning", "error", "success",
                 "foreground", "background", "surface", "panel", "boost"):
        value = getattr(theme, name, None)
        if value is not None:
            colours[name] = value.upper()
    colours.update({k: str(v).upper() for k, v in theme.variables.items() if str(v).startswith("#")})

    bad = {name: value for name, value in colours.items() if value not in ALLOWED_HEX}
    assert not bad, f"non-monochrome theme variables: {bad}"


def test_theme_is_dark_and_flat():
    """``luminosity_spread`` must be 0: any spread generates greys."""

    theme = theme_mod.mono_theme()
    assert theme.dark is True
    assert theme.luminosity_spread == 0.0
    assert theme.text_alpha == 1.0


# ---------------------------------------------------------------------------
# the source
# ---------------------------------------------------------------------------
def _colour_tags_in(path: Path) -> list:
    hits = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in COLOUR_TAG_RE.finditer(line):
            hits.append(f"{path.name}:{number}: {match.group(0)}")
    return hits


def test_no_rich_colour_markup_in_the_ui():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        offenders.extend(_colour_tags_in(path))
    assert not offenders, "colour markup found (use bold/reverse and ASCII markers):\n" + "\n".join(offenders)


def test_the_colour_linter_can_actually_fail():
    """Self-check: a guard that cannot fail is worse than no guard."""

    sample = 'yield Static("[b][red]boom[/red][/b]")\nyield Static("[#FF0000]boom[/#FF0000]")\n'
    tmp = Path(__file__).with_name("_mono_selftest.py")
    try:
        tmp.write_text(sample, encoding="utf-8")
        assert _colour_tags_in(tmp), "the colour markup linter failed to catch [red]"
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# old configuration must not be able to break the palette
# ---------------------------------------------------------------------------
def test_legacy_navy_config_is_ignored():
    cfg = load_config(overrides={"ui": {"background": "#002B49", "accent": "#4CC2FF"}})
    css = theme_mod.build_css(cfg)
    literals = _colour_literals(css)
    assert literals <= ALLOWED_HEX, f"legacy colours leaked into the stylesheet: {literals}"
    # The theme ignores cfg entirely, which is the point.
    assert theme_mod.mono_theme().background.upper() == theme_mod.MONO_BG


def test_unknown_config_keys_do_not_crash():
    """A stale config.yaml key must warn, not kill the app on tty1."""

    cfg = load_config(overrides={"ui": {"this_key_does_not_exist": 1}})
    assert cfg.ui.background == theme_mod.MONO_BG


def test_sgr_allow_list_is_internally_consistent():
    assert not (theme_mod.MONO_SGR_ALLOWED & theme_mod.MONO_SGR_BANNED)
    # 90/100 are the "bright black" pair that made the header invisible.
    assert 90 in theme_mod.MONO_SGR_BANNED and 100 in theme_mod.MONO_SGR_BANNED
    assert 38 in theme_mod.MONO_SGR_BANNED and 48 in theme_mod.MONO_SGR_BANNED


@pytest.mark.parametrize("hex_value", sorted(ALLOWED_HEX))
def test_allowed_hex_values_parse(hex_value):
    assert len(hex_value) == 7
