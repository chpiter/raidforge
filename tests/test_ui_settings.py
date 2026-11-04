"""SettingsPage: split evidence/webplane fields and immediate reconnect."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("textual", reason="needs Textual to run the app headless")

import yaml  # noqa: E402
from raidforge import config as config_mod  # noqa: E402
from raidforge.agent import config as agent_config  # noqa: E402
from raidforge.ui.app import create_app  # noqa: E402
from raidforge.ui.models import PageId  # noqa: E402
from raidforge.ui.pages.settings import SettingsPage  # noqa: E402
from raidforge.ui.widgets.message_panel import MessagePanel  # noqa: E402
from textual.widgets import Input  # noqa: E402

from test_ui_shell import _install_fake_scan, _nav_to, _state  # noqa: E402


class _Runtime:
    def __init__(self, apply: bool = True) -> None:
        self.calls = []
        self.apply = apply

    def active_task(self):
        return None if self.apply else object()

    def request_webplane(self, url, prefix=None):
        self.calls.append((url, prefix))
        return self.apply


def _prepare(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    state.cfg.report.transport = "http"
    state.cfg.report.ssh_host = "192.168.40.129"
    state.cfg.webplane.host = "192.168.40.132"
    state.cfg.webplane.port = 8080
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(tmp_path / "90-site.yaml"))
    monkeypatch.setattr(agent_config, "DEFAULT_RUN_ENV", tmp_path / "agent.env")
    monkeypatch.setattr(SettingsPage, "_probe_values", lambda self, values: None)
    return state


async def _open_settings(pilot, app):
    await pilot.pause(0.6)
    await _nav_to(pilot, app, PageId.SETTINGS)
    await pilot.pause(0.3)
    return app.screen.query_one(SettingsPage)


def test_save_splits_fields_persists_and_reconnects(monkeypatch, tmp_path):
    state = _prepare(monkeypatch, tmp_path)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            page = await _open_settings(pilot, app)
            state.agent_runtime = _Runtime()
            page.query_one("#settings-http-host", Input).value = "evidence.local"
            page.query_one("#settings-webplane-host", Input).value = "10.0.0.9"
            page._save_connection()
            await pilot.pause(0.1)

            data = yaml.safe_load(Path(config_mod.SITE_OVERRIDE_PATH).read_text(encoding="utf-8"))
            assert data["report"]["http_host"] == "evidence.local"
            assert data["report"]["http_base_url"] == ""
            assert data["webplane"]["host"] == "10.0.0.9"
            assert data["webplane"]["port"] == 8080
            assert state.cfg.report.http_host == "evidence.local"
            assert state.agent_runtime.calls == [("http://10.0.0.9:8080", "/api/raidforge")]
            env_text = Path(agent_config.DEFAULT_RUN_ENV).read_text(encoding="utf-8")
            assert "RF_AGENT_SERVER_URL=http://10.0.0.9:8080" in env_text

    asyncio.run(scenario())


def test_invalid_ssh_host_blocks_save(monkeypatch, tmp_path):
    state = _prepare(monkeypatch, tmp_path)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            page = await _open_settings(pilot, app)
            state.agent_runtime = _Runtime()
            page.query_one("#settings-ssh-host", Input).value = "not-an-ip"
            page._save_connection()
            await pilot.pause(0.1)
            assert "IPv4" in str(page._error.content)
            assert not Path(config_mod.SITE_OVERRIDE_PATH).exists()
            assert state.agent_runtime.calls == []

    asyncio.run(scenario())


def test_busy_agent_defers_reconnect_message(monkeypatch, tmp_path):
    state = _prepare(monkeypatch, tmp_path)
    state.agent_runtime = _Runtime(apply=False)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            page = await _open_settings(pilot, app)
            state.agent_runtime = _Runtime(apply=False)
            page.query_one("#settings-webplane-host", Input).value = "10.0.0.9"
            page._save_connection()
            await pilot.pause(0.2)
            panel = app.screen.query_one(MessagePanel)
            assert "current task finishes" in panel.content.plain

    asyncio.run(scenario())
