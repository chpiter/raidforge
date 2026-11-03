"""Monochrome guard, live half: inspect what the app really renders.

The static guard (``test_mono_static.py``) proves the stylesheet only mentions
black and white.  This one renders the four screens and inspects the *resolved*
output, which is the only way to catch a leak from a widget's own built-in CSS,
a Button ``variant`` or a theme variable that was never overridden.

Two candidate sources were rejected before settling on this one:

* the raw terminal stream - Textual's headless driver discards writes
  (``HeadlessDriver.write`` is a no-op), so there is nothing to inspect;
* the SVG screenshot - Rich's SVG writer adds its own chrome (three coloured
  "window" dots, a #292929 frame, its own default text colour), which would
  drown the signal.

What the compositor hands to the terminal is the real thing: a list of styled
segments.  Every foreground/background colour in there must be pure black or
pure white.

Skipped when Textual is not installed (a bare Windows checkout has no Textual;
the build host and the ISO chroot do).
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="live palette check needs Textual")

import textual  # noqa: E402

#: The shipped wheel (see app/requirements.txt).  Textual's widget/CSS behaviour
#: changes between releases, so a frame captured from a newer dev environment
#: proves nothing about the appliance.  A local 8.x install used to fail these
#: two tests for reasons that do not exist inside the ISO.
PINNED_TEXTUAL = "0.89.1"
needs_pinned_textual = pytest.mark.skipif(
    getattr(textual, "__version__", "") != PINNED_TEXTUAL,
    reason=f"verified against textual {PINNED_TEXTUAL}; running {getattr(textual, '__version__', '?')}",
)

from raidforge.config import load_config  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.models import DiskInfo, Medium, Protocol, Vendor  # noqa: E402
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.ui.app import create_app  # noqa: E402
from raidforge.ui.state import AppContext  # noqa: E402
from raidforge.ui.theme import MONO_THEME_NAME  # noqa: E402

ALLOWED_TRIPLETS = {(0, 0, 0), (255, 255, 255)}


def _segments(app) -> list:
    """Flatten the current screen into ``(text, style)`` pairs."""

    update = app.screen._compositor.render_full_update()
    strips = getattr(update, "strips", None) or update
    collected = []
    for strip in strips:
        for segment in getattr(strip, "_segments", []):
            collected.append((segment.text, segment.style))
    return collected


def _bad_colours(segments) -> set:
    """Return the non-monochrome colours present in a frame."""

    offenders = set()
    for _text, style in segments:
        if style is None:
            continue
        for attribute in ("color", "bgcolor"):
            colour = getattr(style, attribute, None)
            triplet = getattr(colour, "triplet", None)
            if triplet is None:
                continue
            if tuple(triplet) not in ALLOWED_TRIPLETS:
                offenders.add((attribute, str(triplet)))
    return offenders


async def _capture_screens():
    """Render every screen; return ``(theme name, {screen: (bad colours, count)})``."""

    cfg = load_config(
        overrides={
            "evidence": {"root": "/tmp/mono-live-evidence"},
            "report": {"enabled": False},
            "tools": {"command_timeout": 5},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-MONO-TEST"})
    ev = EvidenceSession(cfg, sid.serial)
    ctx = AppContext(cfg=cfg, evidence=ev, server_id=sid)
    ctx.disks = [
        DiskInfo(
            disk_id="c0e32s0", controller_index=0, vendor=Vendor.LSI,
            medium=Medium.HDD, protocol=Protocol.SAS, enclosure=32, slot=0,
            model="ST2000NM0008", serial="ZTN1", size_bytes=2000398934016,
            raid_state="Onln",
        ),
        DiskInfo(
            disk_id="c0e32s1", controller_index=0, vendor=Vendor.LSI,
            medium=Medium.SSD, protocol=Protocol.SATA, enclosure=32, slot=1,
            model="S4510", raw={"SED": "Y"},
        ),
    ]
    app = create_app(ctx)
    frames = {}

    def snap(name: str) -> None:
        segments = _segments(app)
        frames[name] = (_bad_colours(segments), len(segments))

    async with app.run_test(size=(80, 25)) as pilot:
        await pilot.pause(0.4)
        snap("dashboard")

        await pilot.press("enter")           # DiskList
        await pilot.pause(0.3)
        snap("disk-list")

        await pilot.press("space")           # select row 0
        await pilot.pause(0.15)
        await pilot.press("enter")           # ConfirmModal
        await pilot.pause(0.3)
        snap("confirm")

        for char in "yes":                   # unlock
            await pilot.press(char)
        await pilot.press("enter")           # Progress
        await pilot.pause(0.6)
        snap("progress")

        await pilot.press("escape")
        await pilot.pause(0.2)
        await pilot.press("escape")          # DiskList -> Dashboard
        await pilot.pause(0.2)
        await pilot.press("s")               # Settings
        await pilot.pause(0.3)
        snap("settings")
        await pilot.press("escape")
        await pilot.pause(0.2)
        theme_name = app.theme

    return theme_name, frames


@needs_pinned_textual
def test_only_black_and_white_are_rendered():
    import asyncio

    theme_name, frames = asyncio.run(_capture_screens())
    assert theme_name == MONO_THEME_NAME, f"theme is {theme_name!r}, expected {MONO_THEME_NAME!r}"

    problems = [f"{name}: {sorted(colours)}" for name, (colours, _count) in frames.items() if colours]
    assert not problems, "non-monochrome colours rendered:\n" + "\n".join(problems)


@needs_pinned_textual
def test_every_screen_renders_something():
    """A blank frame would make the palette check vacuously pass."""

    import asyncio

    _theme, frames = asyncio.run(_capture_screens())
    for name, (_colours, count) in frames.items():
        assert count > 20, f"{name} produced only {count} styled segments"


def test_the_colour_scanner_can_actually_fail():
    """Self-check: a leaked colour must be reported, not ignored."""

    from rich.style import Style

    offenders = _bad_colours([("boom", Style.parse("#4CC2FF on #000000"))])
    assert offenders, "the colour scanner missed a cyan foreground"
