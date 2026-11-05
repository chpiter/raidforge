from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("textual")
from textual.app import App  # noqa: E402
from textual.widgets import Input  # noqa: E402

from raidforge.agent.models import ConfirmRequest  # noqa: E402
from raidforge.ui.screens.remote_task_confirm import RemoteTaskConfirmScreen  # noqa: E402


def _request(timeout: int = 60) -> ConfirmRequest:
    return ConfirmRequest(
        task_id="tsk_01JB8Z4Q2M",
        task_type="create_raid",
        server_sn="SN12345678",
        created_by="alice",
        summary=["type create_raid"],
        targets=["ZA1 / 0x5000c500a1b2c3d4 / 100 bytes"],
        commands=["storcli64 /c0/e32/s4 start erase normal"],
        topology_hash="9f2c" + "0" * 60,
        timeout_seconds=timeout,
        default_operator="tester",
    )


class _App(App):
    def __init__(self, request: ConfirmRequest) -> None:
        super().__init__()
        self.request = request
        self.decision = None

    def on_mount(self) -> None:
        self.push_screen(RemoteTaskConfirmScreen(self.request), self._done)

    def _done(self, decision) -> None:
        self.decision = decision


def test_exact_yes_is_accepted():
    async def scenario() -> None:
        request = _request()
        app = _App(request)
        async with app.run_test() as pilot:
            screen = next(s for s in app.screen_stack if isinstance(s, RemoteTaskConfirmScreen))
            screen.query_one("#remote-confirm-ack", Input).value = "yes"
            screen.action_confirm()
            await pilot.pause()
        assert app.decision is not None
        assert app.decision.approved is True
        assert app.decision.operator == "tester"

    asyncio.run(scenario())


def test_non_exact_yes_is_rejected_then_escape_declines():
    async def scenario() -> None:
        request = _request()
        app = _App(request)
        async with app.run_test() as pilot:
            screen = next(s for s in app.screen_stack if isinstance(s, RemoteTaskConfirmScreen))
            for value in ("Yes", "YES", "y", "Y", "ok", "是", "", " yes", "yes ", "yes\n", "true"):
                screen.query_one("#remote-confirm-ack", Input).value = value
                screen.action_confirm()
                await pilot.pause()
                assert app.decision is None
            screen.action_reject()
            await pilot.pause()
        assert app.decision is not None
        assert app.decision.approved is False
        assert app.decision.reason == "confirm_rejected"

    asyncio.run(scenario())


def test_timeout_produces_confirm_timeout():
    async def scenario() -> None:
        request = _request(timeout=60)
        app = _App(request)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = next(s for s in app.screen_stack if isinstance(s, RemoteTaskConfirmScreen))
            screen._deadline = time.monotonic() - 1
            screen._tick()
            await pilot.pause()
        assert app.decision is not None
        assert app.decision.reason == "confirm_timeout"

    asyncio.run(scenario())
