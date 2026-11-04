"""Palette guards for the two-theme design.

The deep-blue theme is the 7.2 default.  The monochrome theme is the
console-safe fallback: pure black and white only, selected automatically on
``TERM=linux``.
"""

from __future__ import annotations

import re
from pathlib import Path

from raidforge.config import load_config
from raidforge.ui import palette as palette_mod
from raidforge.ui import theme as theme_mod

APP_DIR = Path(__file__).resolve().parent.parent / "app" / "raidforge"
MONO_ALLOWED = {"#000000", "#FFFFFF"}

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")
COLOUR_TAG_RE = re.compile(
    r"\[/?(?:[a-z]+\s+)*(red|green|blue|cyan|magenta|yellow|orange|purple|white|black"
    r"|#[0-9a-fA-F]{3,6}|on\s+[a-z]+)\]",
    re.IGNORECASE,
)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _colour_literals(text: str) -> set:
    without_comments = CSS_COMMENT_RE.sub("", text)
    return {value.upper() for value in HEX_RE.findall(without_comments)}


def test_tcss_uses_only_theme_variables():
    css = theme_mod.build_css(load_config())
    literals = _colour_literals(css)
    assert not literals, f"hex colours belong in palette.py, found: {literals}"
    for variable in ("$background", "$accent", "$error", "$nav-active-bg", "$table-zebra"):
        assert variable in css


def test_deep_blue_theme_has_the_required_colours():
    theme = palette_mod.raidforge_theme()
    assert theme.background.upper() == palette_mod.BLUE_BG
    assert theme.accent.upper() == palette_mod.BLUE_ACCENT
    assert theme.error.upper() == palette_mod.BLUE_DANGER
    assert theme.variables["nav-active-bg"] == "#0066A1"
    assert theme.variables["table-zebra"].upper() == palette_mod.BLUE_ZEBRA


def test_mono_theme_is_black_and_white_only():
    theme = palette_mod.mono_theme()
    colours = {}
    for name in (
        "primary",
        "secondary",
        "accent",
        "warning",
        "error",
        "success",
        "foreground",
        "background",
        "surface",
        "panel",
        "boost",
    ):
        value = getattr(theme, name, None)
        if value is not None:
            colours[name] = value.upper()
    colours.update(
        {k: str(v).upper() for k, v in theme.variables.items() if str(v).startswith("#")}
    )
    bad = {name: value for name, value in colours.items() if value not in MONO_ALLOWED}
    assert not bad, f"non-monochrome mono theme variables: {bad}"


def test_console_fallback_picks_mono():
    assert (
        palette_mod.select_theme_name("auto", {"TERM": "linux"})
        == palette_mod.THEME_MONO
    )
    assert (
        palette_mod.select_theme_name("auto", {"TERM": "xterm-256color"})
        == palette_mod.THEME_BLUE
    )
    assert (
        palette_mod.select_theme_name("auto", {"TERM": "xterm", "RAIDFORGE_THEME": "mono"})
        == palette_mod.THEME_MONO
    )
    assert (
        palette_mod.select_theme_name("raidforge-mono", {"TERM": "xterm-256color"})
        == palette_mod.THEME_MONO
    )


def test_theme_is_dark_and_flat():
    for theme in (palette_mod.raidforge_theme(), palette_mod.mono_theme()):
        assert theme.dark is True
        assert theme.luminosity_spread == 0.0
        assert theme.text_alpha == 1.0


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
    assert not offenders, "colour markup found (use Text styles from palette.py):\n" + "\n".join(
        offenders
    )


def test_the_colour_linter_can_actually_fail():
    sample = 'yield Static("[b][red]boom[/red][/b]")\nyield Static("[#FF0000]boom[/#FF0000]")\n'
    tmp = Path(__file__).with_name("_mono_selftest.py")
    try:
        tmp.write_text(sample, encoding="utf-8")
        assert _colour_tags_in(tmp), "the colour markup linter failed to catch [red]"
    finally:
        tmp.unlink(missing_ok=True)


def test_legacy_ui_colours_do_not_override_the_theme():
    cfg = load_config(overrides={"ui": {"background": "#123456", "accent": "#654321"}})
    # The stylesheet is variable-only; the theme object comes from palette.py.
    assert not _colour_literals(theme_mod.build_css(cfg))
    assert palette_mod.raidforge_theme().background.upper() == palette_mod.BLUE_BG


def test_unknown_config_keys_do_not_crash():
    cfg = load_config(overrides={"ui": {"this_key_does_not_exist": 1}})
    assert cfg.ui.theme in ("auto", "raidforge", "raidforge-mono")


def test_sgr_allow_list_is_internally_consistent():
    assert not (theme_mod.MONO_SGR_ALLOWED & theme_mod.MONO_SGR_BANNED)
    assert 90 in theme_mod.MONO_SGR_BANNED and 100 in theme_mod.MONO_SGR_BANNED
    assert 38 in theme_mod.MONO_SGR_BANNED and 48 in theme_mod.MONO_SGR_BANNED
