"""Regression: Q after a *finished* erase run must exit, not fake a cancel.

The live failure (qemu artifacts ``06-after-quit`` == ``07-still-running``):
the run had already completed when Q was pressed, but ``action_cancel_run``
still took the cancel branch - it set ``cancel_event``, painted
"cancelling - waiting for the current command", and nothing ever cleared that
state because the worker was long gone.  The screen looked frozen.

The contract now is:

* run active      -> first Q cancels, second Q exits;
* run finished    -> first Q exits immediately;
* other screens   -> Q exits immediately (unchanged).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="legacy progress screen; task queue covered by tests/test_ui_shell.py")

pytest.importorskip("textual", reason="needs Textual to run the app headless")

import textual  # noqa: E402

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
from raidforge.ui.screens.progress import ProgressScreen  # noqa: E402
from raidforge.ui.state import AppContext  # noqa: E402


def _disk(disk_id: str, medium: Medium) -> DiskInfo:
    return DiskInfo(
        disk_id=disk_id,
        controller_index=0,
        vendor=Vendor.LSI,
        medium=medium,
        protocol=Protocol.SAS,
        enclosure=32,
        slot=0,
        model="ST2000NM0008",
        serial=disk_id.upper(),
        size_bytes=2000398934016,
        raid_state="Onln",
    )


def _context() -> AppContext:
    cfg = load_config(
        overrides={
            "evidence": {"root": "/tmp/quit-path-evidence"},
            "report": {"enabled": False},
            "tools": {"command_timeout": 5},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-QUIT-TEST"})
    ev = EvidenceSession(cfg, sid.serial)
    ctx = AppContext(cfg=cfg, evidence=ev, server_id=sid)
    ctx.disks = [_disk("c0e32s0", Medium.HDD)]
    # NB: do NOT pre-fill ctx.selection - DiskList then starts with the row
    # ticked and the "space" keystroke below would *deselect* it, so the
    # confirm dialog never opens.  Drive the UI like a real operator instead.
    return ctx


def _run_finished(app) -> bool:
    return (
        isinstance(app.screen, ProgressScreen)
        and not app.screen.is_run_active()
    )


@needs_pinned_textual
def test_q_after_finished_run_exits_the_app():
    """Q on a finished progress screen must terminate the app."""

    import asyncio

    async def scenario() -> None:
        app = create_app(_context())
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("enter")  # Dashboard -> DiskList
            await pilot.pause(0.3)
            await pilot.press("space")  # select row
            await pilot.pause(0.1)
            await pilot.press("enter")  # ConfirmModal
            await pilot.pause(0.3)
            for char in "yes":
                await pilot.press(char)
            await pilot.press("enter")  # -> Progress
            await pilot.pause(0.3)

            # Wait (bounded) for the dry/mock run to drain.
            for _ in range(80):
                if _run_finished(app):
                    break
                await pilot.pause(0.1)
            assert _run_finished(app), "run never finished - test premise broken"

            await pilot.press("q")
            await pilot.pause(0.5)

            # If the bug were back, the app would still be alive, parked on a
            # "cancelling" message no worker will ever clear.
            assert app.return_code == 0, (
                "app did not exit on Q after the run finished - "
                f"screen is {app.screen!r}"
            )

    asyncio.run(scenario())


@needs_pinned_textual
def test_q_while_running_cancels_first_then_exits():
    """First Q during a live run only cancels; the screen must recover."""

    import asyncio

    async def scenario() -> None:
        app = create_app(_context())
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("space")
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.3)
            for char in "yes":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause(0.05)  # grab the screen while the run is live

            assert isinstance(app.screen, ProgressScreen)
            await pilot.press("q")  # cancel branch
            await pilot.pause(0.2)

            # Either the run was cancelled and the screen recovered with the
            # final summary, or it had already finished before our Q landed -
            # both are sane.  What must never happen is a permanent
            # "cancelling" state with the worker gone.
            for _ in range(80):
                if not app.screen.is_run_active():
                    break
                await pilot.pause(0.1)
            assert not app.screen.is_run_active()
            # The screen must have *recovered*: the final summary replaces the
            # interim "cancelling - waiting for the current command" line.
            recovered = app.ctx.last_message
            assert "cancelling" not in recovered.lower(), (
                f"screen stuck on the cancel message: {recovered!r}"
            )
            # A second Q exits.
            await pilot.press("q")
            await pilot.pause(0.5)
            assert app.return_code == 0, f"second Q did not exit - screen {app.screen!r}"

    asyncio.run(scenario())
