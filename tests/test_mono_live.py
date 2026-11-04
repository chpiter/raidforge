"""Live palette checks: the mono theme has no colour, the blue theme does.

The compositor segments are the real thing Textual would send to the terminal;
the SVG writer adds its own chrome, so it is not used here.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

pytest.importorskip("textual", reason="live palette check needs Textual")

from rich.style import Style  # noqa: E402

from raidforge.config import load_config  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.ui.state import AppState  # noqa: E402

MONO_ALLOWED = {(0, 0, 0), (255, 255, 255)}


def _flatten(strip):
    segments = getattr(strip, "_segments", None)
    if segments is not None:
        for segment in segments:
            yield segment
        return
    try:
        children = list(strip)
    except TypeError:
        return
    for child in children:
        yield from _flatten(child)


def _segments(app) -> list:
    update = app.screen._compositor.render_full_update()
    strips = getattr(update, "strips", None) or update
    collected = []
    for strip in strips:
        for segment in _flatten(strip):
            collected.append((segment.text, segment.style))
    return collected


def _colour_triplets(segments) -> set:
    found = set()
    for _text, style in segments:
        if style is None:
            continue
        for attribute in ("color", "bgcolor"):
            colour = getattr(style, attribute, None)
            triplet = getattr(colour, "triplet", None)
            if triplet is not None:
                found.add(tuple(triplet))
    return found


def _make_state() -> AppState:
    tmp = tempfile.mkdtemp()
    cfg = load_config(
        overrides={
            "evidence": {"root": tmp + "/evidence"},
            "report": {"enabled": False},
            "tools": {"command_timeout": 2},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-MONO-TEST"})
    return AppState(cfg=cfg, evidence=EvidenceSession(cfg, sid.serial), server_id=sid)


async def _capture(theme: str):
    os.environ["RAIDFORGE_THEME"] = theme
    try:
        from raidforge.ui.app import create_app

        state = _make_state()
        app = create_app(state)
        frames = {}
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.4)
            for index in range(1, 8):
                await pilot.press(f"ctrl+{index}")
                await pilot.pause(0.15)
                segments = _segments(app)
                frames[f"page-{index}"] = (segments, _colour_triplets(segments))
            return app.theme, frames
    finally:
        os.environ.pop("RAIDFORGE_THEME", None)


def test_mono_theme_renders_only_black_and_white():
    theme, frames = asyncio.run(_capture("raidforge-mono"))
    assert theme == "raidforge-mono"
    for name, (_segments, colours) in frames.items():
        bad = colours - MONO_ALLOWED
        assert not bad, f"{name} rendered non-monochrome colours: {bad}"


def test_blue_theme_renders_the_72_palette():
    theme, frames = asyncio.run(_capture("raidforge"))
    assert theme == "raidforge"
    all_colours = set()
    for _segments, colours in frames.values():
        all_colours |= colours
    # #4FC3F7 accent or #002B49 background must be present somewhere.
    assert (79, 195, 247) in all_colours or (0, 43, 73) in all_colours, all_colours


def test_every_page_renders_something():
    _theme, frames = asyncio.run(_capture("raidforge"))
    for name, (segments, _colours) in frames.items():
        assert len(segments) > 20, f"{name} produced only {len(segments)} styled segments"


def test_the_colour_scanner_can_actually_fail():
    assert _colour_triplets([("boom", Style.parse("#4CC2FF on #000000"))])
