"""Monochrome theme: pure black and pure white, nothing else.

Why so strict
-------------
The appliance runs on the bare Linux console (tty1 / fbcon), which has **16
ANSI colours only**.  Anything that is not pure black or pure white gets
quantised on the way out, and a dark brand colour such as ``#002B49`` lands on
``SGR 90`` ("bright black"), which several VGA/fbcon implementations then draw
as nothing at all on top of a coloured background.  That is how the disk table
header turned into a blank cyan bar with no visible text, while every headless
test on a 256-colour terminal stayed green.

So the palette is not configurable any more: it is a constant.  Hierarchy is
expressed with **bold** and **reverse video**, and state with ASCII markers
(``>` ``, ``[X]``, ``OK``, ``FAIL``, ``!!``), all of which survive any terminal.

Layout contract
---------------
* the top status bar is docked to the top and never scrolls;
* the bottom key bar is docked to the bottom and never scrolls;
* everything in between is one vertically scrolling content area.
"""

from __future__ import annotations

from typing import Any

#: The only two colours allowed anywhere in the UI or on the wire.
MONO_BG = "#000000"
MONO_FG = "#FFFFFF"

#: Name of the Textual theme registered by :func:`mono_theme`.
MONO_THEME_NAME = "raidforge-mono"

#: SGR parameters that may appear in the terminal byte stream.  Everything else
#: is treated as a colour leak by ``tests/test_mono_live.py``.
#: ``90``/``100`` (bright black) are deliberately *not* here: that is the code
#: that made the header text invisible on fbcon.  38/48 (extended colour) are
#: banned for the same reason.
MONO_SGR_ALLOWED = frozenset(
    {
        "", 0, 1, 22, 7, 27, 39, 49,  # reset, bold, reverse, defaults
        30, 37, 40, 47,               # black/white fg/bg (8-colour terminals)
        97, 107,                      # bright white fg/bg (#FFFFFF on 16-colour)
    }
)

#: SGR parameters that must never appear (kept explicit for error messages).
MONO_SGR_BANNED = frozenset({2, 3, 4, 5, 9, 38, 48, 90, 100,
                             31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96, 101})

#: Variables that must resolve to the background colour.
_BG_VARS = (
    "surface", "panel", "boost", "secondary-muted", "block-hover-background",
    "block-cursor-blurred-background",
    "input-selection-background",
    "scrollbar-background", "scrollbar-background-hover", "scrollbar-background-active",
    "scrollbar-corner-color",
    "footer-background", "footer-key-background", "footer-description-background",
    "footer-item-background", "link-background", "link-background-hover",
    "surface-darken-1", "surface-lighten-1", "surface-lighten-2", "surface-lighten-3",
    "panel-darken-1", "panel-darken-2", "panel-lighten-1",
    "boost-darken-1", "boost-lighten-1",
    "primary-darken-2", "primary-darken-3", "accent-darken-1",
    "error-darken-1", "error-darken-2", "error-darken-3",
    "success-darken-2", "success-darken-3", "warning-darken-1",
    "warning-darken-2", "warning-darken-3",
)

#: Variables that must resolve to the foreground colour.
_FG_VARS = (
    "foreground", "text", "primary", "secondary", "accent", "success", "warning", "error",
    "text-muted", "text-disabled", "text-primary", "text-secondary", "text-accent",
    "text-success", "text-warning", "text-error",
    "primary-muted", "accent-muted", "success-muted", "warning-muted", "error-muted",
    "primary-lighten-3", "accent-lighten-1",
    "error-lighten-2", "success-lighten-1", "success-lighten-2", "warning-lighten-2",
    "border", "border-blurred",
    "block-cursor-background", "block-cursor-blurred-foreground",
    "input-cursor-background",
    "button-foreground", "button-color-foreground",
    "link-color", "link-color-hover",
    "scrollbar", "scrollbar-hover", "scrollbar-active",
    "footer-foreground", "footer-key-foreground", "footer-description-foreground",
)

#: ``*-text-style`` variables: plain strings, not colours.
_STYLE_VARS = {
    "block-cursor-text-style": "bold",
    "block-cursor-blurred-text-style": "bold",
    "input-cursor-text-style": "bold",
    "button-focus-text-style": "bold reverse",
}


def mono_theme():
    """Build the Textual ``Theme`` object for the appliance.

    Imported lazily so that the pure-static tests (and any tool that only wants
    the CSS text) do not need Textual installed.
    """

    from textual.theme import Theme

    variables = {name: MONO_BG for name in _BG_VARS}
    variables.update({name: MONO_FG for name in _FG_VARS})
    variables.update(_STYLE_VARS)
    return Theme(
        name=MONO_THEME_NAME,
        primary=MONO_FG,
        secondary=MONO_FG,
        accent=MONO_FG,
        warning=MONO_FG,
        error=MONO_FG,
        success=MONO_FG,
        foreground=MONO_FG,
        background=MONO_BG,
        surface=MONO_BG,
        panel=MONO_BG,
        boost=MONO_BG,
        dark=True,
        luminosity_spread=0.0,
        text_alpha=1.0,
        variables=variables,
    )


DEFAULT_CSS = """
Screen {
    background: $background;
    color: $foreground;
    layers: base;
}

/* Inverted bar: white background, black text.  The children set their own
   colour, so they have to be inverted too - otherwise it is white on white,
   which is precisely the class of bug this palette exists to avoid. */
#status-bar {
    dock: top;
    height: 3;
    width: 100%;
    layout: horizontal;
    background: $foreground;
    color: $background;
    border-bottom: none;
    padding: 0 1;
}

#status-bar > Static {
    width: 1fr;
    height: 1;
    color: $background;
}

#status-bar .kv-key,
#status-bar .kv-value {
    color: $background;
}

#status-bar > .sb-right {
    text-align: right;
}

#key-bar {
    dock: bottom;
    height: auto;
    max-height: 3;
    width: 100%;
    background: $background;
    color: $foreground;
    border-top: solid $foreground;
    padding: 0 1;
}

#content {
    height: 1fr;
    width: 100%;
    padding: 1 2;
    overflow-y: auto;
}

.title {
    color: $foreground;
    text-style: bold;
}

/* "muted" no longer means "another colour": hierarchy is bold vs normal. */
.muted {
    color: $foreground;
    text-style: none;
}

.danger {
    color: $foreground;
    text-style: bold reverse;
}

.ok {
    color: $foreground;
    text-style: bold;
}

.section {
    height: auto;
    margin: 1 0;
}

.kv-row {
    height: auto;
    layout: horizontal;
}

.kv-key {
    width: 22;
    color: $foreground;
}

.kv-value {
    width: 1fr;
    color: $foreground;
}

DataTable {
    height: 1fr;
    background: $background;
    color: $foreground;
}

/* Header: a white band with black text.  Pure #000000 maps to SGR 30 on every
   terminal; a dark brand colour degrades to SGR 90 and disappears. */
DataTable > .datatable--header {
    background: $foreground;
    color: $background;
    text-style: bold;
}

/* The row cursor is the ">" marker column painted by widgets/disk_table.py.
   Textual's own cursor component is neutralised here: on a 16-colour console it
   only paints the last cell of the row, which reads as a rendering fault. */
DataTable > .datatable--cursor,
DataTable:focus > .datatable--cursor {
    background: $background;
    color: $foreground;
    text-style: bold;
}

DataTable > .datatable--hover {
    background: $background;
}

/* Textual tints a focused widget with 5% foreground, which on a black
   background produces a near-black grey - neither black nor white, i.e. exactly
   the kind of value that gets mangled on a 16-colour console.  Focus is shown
   by the cursor row and the form border instead. */
DataTable:focus,
DataTable:focus > .datatable--header,
DataTable:focus > .datatable--cursor,
Input:focus,
Button:focus {
    background-tint: 0%;
}

Input {
    margin: 1 0;
    background: $background;
    color: $foreground;
    border: tall $foreground;
}

Input:focus {
    border: double $foreground;
}

Input.-invalid,
Input.-invalid:focus {
    border: double $foreground;
}

/* Textual dims the placeholder; on a 16-colour console "dim" is a different
   colour, not a lighter shade, so it would be a third palette entry. */
Input > .input--placeholder {
    color: $foreground;
    text-style: none;
}

Button {
    margin: 1 2 1 0;
    background: $background;
    color: $foreground;
    border: tall $foreground;
}

Button:hover {
    text-style: reverse;
}

Button:focus {
    text-style: bold reverse;
}

/* Settings transport toggle + receiver/port row.  Heights are fixed so the
   80x25 console does not have to scroll just to reach Save. */
#transport-row,
#host-row {
    height: auto;
}

Button.seg {
    height: 3;
    min-width: 12;
    margin: 0 1 0 0;
}

#host-row #ssh-host {
    width: 1fr;
    margin: 0 1 0 0;
}

#host-row #port {
    width: 18;
    margin: 0;
}

#http-endpoint {
    height: 1;
}

#settings-actions {
    height: auto;
}

#settings-actions Button {
    height: 3;
    margin: 0 1 0 0;
}

/* Button variants carry no colour any more - the labels and the focus
   highlight carry the meaning. */
Button.-primary,
Button.-error,
Button.-success,
Button.-warning,
Button.-default {
    background: $background;
    color: $foreground;
    border: tall $foreground;
}

Button.-primary:hover,
Button.-error:hover,
Button.-success:hover,
Button.-warning:hover,
Button.-default:hover,
Button.-primary:focus,
Button.-error:focus,
Button.-success:focus,
Button.-warning:focus,
Button.-default:focus {
    text-style: bold reverse;
}

/* RAID screens: two tables plus details must fit an 80x25 console without
   hiding the key bar.  The content area scrolls if the controller details are
   longer than the fixed table heights. */
#volume-table {
    height: 7;
}

#pd-table {
    height: 9;
}

#raid-name {
    margin: 0 0 1 0;
}

#log {
    height: auto;
    max-height: 3;
    border: none;
    padding: 0 1;
    overflow-y: auto;
}
"""


def build_css(cfg: Any = None) -> str:
    """Return the application CSS.

    The stylesheet is now colour-free apart from ``$background``/``$foreground``
    (both pure), which come from the registered theme rather than from
    ``config.yaml``.  ``cfg`` is accepted and ignored so existing call sites keep
    working.

    Args:
        cfg: Unused; kept for backwards compatibility.

    Returns:
        The static stylesheet text.
    """

    return DEFAULT_CSS
