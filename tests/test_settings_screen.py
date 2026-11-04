"""Settings screen: preferred transport, receiver IP and port.

The SSH user, key path and remote root are baked into the image; the screen
shows them read-only.  The operator may switch between SSH and HTTP and edit the
port for the selected channel.  Saving must persist to config.d, update the live
config used by the uploader, reject bad IPv4/ports, and probe only the selected
channel.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="legacy settings screen; replaced by tests/test_ui_settings.py")

pytest.importorskip("textual", reason="needs Textual to run the app headless")

import textual  # noqa: E402

from raidforge import config as config_mod  # noqa: E402
from raidforge.config import load_config  # noqa: E402
from raidforge.core import report as report_mod  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.ui.app import create_app  # noqa: E402
from raidforge.ui.screens import settings as settings_mod  # noqa: E402
from raidforge.ui.screens.dashboard import DashboardScreen  # noqa: E402
from raidforge.ui.screens.settings import SettingsScreen  # noqa: E402
from raidforge.ui.state import AppContext  # noqa: E402
from textual.widgets import Button, Input  # noqa: E402

PINNED_TEXTUAL = "0.89.1"
needs_pinned_textual = pytest.mark.skipif(
    getattr(textual, "__version__", "") != PINNED_TEXTUAL,
    reason=f"verified against textual {PINNED_TEXTUAL}; running {getattr(textual, '__version__', '?')}",
)


class _ProbeRecorder:
    """Stand-in for report.check_ssh: records calls, never touches the network."""

    def __init__(self) -> None:
        self.calls = []
        self.result = report_mod.SshCheck(True, "evidence@10.1.2.3:22 reachable, remote root writable")
        self.delay = 0.0

    def __call__(self, cfg, timeout=5.0):
        self.calls.append((cfg.report.ssh_host, timeout))
        if self.delay:
            time.sleep(self.delay)
        return self.result


@pytest.fixture(autouse=True)
def probe(monkeypatch):
    recorder = _ProbeRecorder()
    monkeypatch.setattr(settings_mod.report_mod, "check_ssh", recorder)
    return recorder


class _HttpProbeRecorder:
    """Stand-in for report.check_http: records endpoint + timeout."""

    def __init__(self) -> None:
        self.calls = []
        self.result = report_mod.ProbeCheck(True, "HTTP 200 from http://10.1.2.3:8080")
        self.delay = 0.0

    def __call__(self, cfg, timeout=5.0):
        self.calls.append((report_mod.http_endpoint(cfg), timeout))
        if self.delay:
            time.sleep(self.delay)
        return self.result


@pytest.fixture(autouse=True)
def http_probe(monkeypatch):
    recorder = _HttpProbeRecorder()
    monkeypatch.setattr(settings_mod.report_mod, "check_http", recorder)
    return recorder


def _context(tmp_path: Path, config_dir: Path) -> AppContext:
    cfg = load_config(
        config_dir=str(config_dir),
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {
                "enabled": False,
                "wait_for_route_seconds": 0,
                "ssh_host": "192.168.40.129",
            },
            "tools": {"command_timeout": 5},
        },
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-SETTINGS-TEST"})
    ev = EvidenceSession(cfg, sid.serial)
    return AppContext(cfg=cfg, evidence=ev, server_id=sid)


@needs_pinned_textual
def test_settings_saves_ip_and_keeps_fixed_identity(monkeypatch, tmp_path, probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.delenv("RAIDFORGE_REPORT_SSH_HOST", raising=False)
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            assert isinstance(app.screen, SettingsScreen), type(app.screen)

            field = app.screen.query_one("#ssh-host", Input)
            field.value = "10.1.2.3"
            await pilot.press("enter")
            await pilot.pause(0.5)

            site = Path(config_mod.SITE_OVERRIDE_PATH)
            assert site.is_file(), "settings were not persisted"
            assert "10.1.2.3" in site.read_text(encoding="utf-8")
            assert ctx.cfg.report.ssh_host == "10.1.2.3"
            # Exactly one probe, against the value just saved, with the short
            # settings timeout.
            assert probe.calls == [("10.1.2.3", settings_mod.SSH_CHECK_TIMEOUT)]
            assert "reachable" in ctx.last_message
            # Fixed identity must never be touched by this screen.
            assert ctx.cfg.report.ssh_user == "evidence"
            assert ctx.cfg.report.ssh_key_path == "/etc/raidforge/keys/evidence_ed25519"
            assert ctx.cfg.report.ssh_port == 22
            assert ctx.cfg.report.ssh_remote_root == "/srv/evidence/incoming"

            # Invalid input is refused, does not persist and does not probe.
            field.value = "999.1.1.1"
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, SettingsScreen)
            assert ctx.cfg.report.ssh_host == "10.1.2.3"
            assert "999.1.1.1" not in site.read_text(encoding="utf-8")
            assert len(probe.calls) == 1

            # Back to the Dashboard; the config carried over.
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, DashboardScreen), type(app.screen)
            assert ctx.cfg.report.ssh_host == "10.1.2.3"

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_accepts_empty_to_disable_upload(monkeypatch, tmp_path, probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.delenv("RAIDFORGE_REPORT_SSH_HOST", raising=False)
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            app.screen.query_one("#ssh-host", Input).value = ""
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert ctx.cfg.report.ssh_host == ""
            assert "queue locally" in ctx.last_message
            # Nothing to probe: an empty host disables upload entirely.
            assert probe.calls == []

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_failed_probe_keeps_the_saved_value(monkeypatch, tmp_path, probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.delenv("RAIDFORGE_REPORT_SSH_HOST", raising=False)
    probe.result = report_mod.SshCheck(False, "ssh failed: Connection timed out", "stderr tail")
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            app.screen.query_one("#ssh-host", Input).value = "10.255.255.1"
            await pilot.press("enter")
            await pilot.pause(0.5)

            # Save-then-warn: the value stays everywhere.
            site = Path(config_mod.SITE_OVERRIDE_PATH)
            assert "10.255.255.1" in site.read_text(encoding="utf-8")
            assert ctx.cfg.report.ssh_host == "10.255.255.1"
            assert probe.calls == [("10.255.255.1", settings_mod.SSH_CHECK_TIMEOUT)]
            assert "saved, but SSH check failed" in ctx.last_message
            assert "queue locally" in ctx.last_message
            assert "Connection timed out" in ctx.last_message
            assert "saved, but SSH check failed" in str(app.screen._error.content)

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_repeated_enter_probes_once(monkeypatch, tmp_path, probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.delenv("RAIDFORGE_REPORT_SSH_HOST", raising=False)
    probe.delay = 0.25
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            app.screen.query_one("#ssh-host", Input).value = "10.1.2.3"
            await pilot.press("enter")
            await pilot.press("enter")
            await pilot.pause(0.8)

            assert probe.calls == [("10.1.2.3", settings_mod.SSH_CHECK_TIMEOUT)]

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_saves_ssh_port_and_derived_http_url(monkeypatch, tmp_path, probe, http_probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.delenv("RAIDFORGE_REPORT_SSH_HOST", raising=False)
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            app.screen.query_one("#ssh-host", Input).value = "10.1.2.3"
            app.screen.query_one("#port", Input).value = "2222"
            await pilot.press("enter")
            await pilot.pause(0.5)

            site = Path(config_mod.SITE_OVERRIDE_PATH)
            text = site.read_text(encoding="utf-8")
            assert "transport: ssh" in text
            assert "ssh_port: 2222" in text
            assert "http_port: 8080" in text
            assert "http://10.1.2.3:8080" in text
            assert ctx.cfg.report.ssh_port == 2222
            assert ctx.cfg.report.http_port == 8080
            assert ctx.cfg.report.http_base_url == "http://10.1.2.3:8080"
            assert probe.calls == [("10.1.2.3", settings_mod.SSH_CHECK_TIMEOUT)]
            assert http_probe.calls == []

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_switches_to_http_and_saves_8080(monkeypatch, tmp_path, probe, http_probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.delenv("RAIDFORGE_REPORT_SSH_HOST", raising=False)
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            app.screen.query_one("#ssh-host", Input).value = "10.1.2.3"
            app.screen.query_one("#transport-http", Button).press()
            await pilot.pause(0.2)
            assert app.screen.query_one("#port", Input).value == "8080"
            await pilot.press("enter")
            await pilot.pause(0.5)

            site = Path(config_mod.SITE_OVERRIDE_PATH)
            text = site.read_text(encoding="utf-8")
            assert "transport: http" in text
            assert "http_port: 8080" in text
            assert "http://10.1.2.3:8080" in text
            assert ctx.cfg.report.transport == "http"
            assert ctx.cfg.report.http_base_url == "http://10.1.2.3:8080"
            assert probe.calls == []
            assert http_probe.calls == [
                ("http://10.1.2.3:8080", settings_mod.HTTP_CHECK_TIMEOUT)
            ]
            assert "reachable" in ctx.last_message

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_port_follows_transport_selection(monkeypatch, tmp_path, probe, http_probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            screen = app.screen
            port = screen.query_one("#port", Input)
            port.value = "2222"
            screen.query_one("#transport-http", Button).press()
            await pilot.pause(0.15)
            assert port.value == "8080"
            screen.query_one("#transport-ssh", Button).press()
            await pilot.pause(0.15)
            assert port.value == "2222", "unsaved SSH port was not buffered"

    asyncio.run(scenario())


@needs_pinned_textual
@pytest.mark.parametrize("bad_port", ["0", "65536", "abc", ""])
def test_settings_rejects_bad_port(monkeypatch, tmp_path, probe, http_probe, bad_port):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            app.screen.query_one("#ssh-host", Input).value = "10.1.2.3"
            app.screen.query_one("#port", Input).value = bad_port
            await pilot.press("enter")
            await pilot.pause(0.3)

            assert not Path(config_mod.SITE_OVERRIDE_PATH).exists()
            assert ctx.cfg.report.ssh_port == 22
            assert probe.calls == [] and http_probe.calls == []
            assert "port must be" in str(app.screen._error.content)

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_http_probe_failure_keeps_config(monkeypatch, tmp_path, probe, http_probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.delenv("RAIDFORGE_REPORT_SSH_HOST", raising=False)
    http_probe.result = report_mod.ProbeCheck(False, "http failed: connection refused")
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            app.screen.query_one("#ssh-host", Input).value = "10.1.2.3"
            app.screen.query_one("#transport-http", Button).press()
            await pilot.pause(0.15)
            await pilot.press("enter")
            await pilot.pause(0.5)

            assert ctx.cfg.report.transport == "http"
            assert ctx.cfg.report.http_base_url == "http://10.1.2.3:8080"
            assert "saved, but HTTP check failed" in ctx.last_message
            assert "queue locally" in ctx.last_message
            assert "connection refused" in ctx.last_message

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_disables_controls_during_probe(monkeypatch, tmp_path, probe, http_probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.delenv("RAIDFORGE_REPORT_SSH_HOST", raising=False)
    probe.delay = 0.3
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            app.screen.query_one("#ssh-host", Input).value = "10.1.2.3"
            await pilot.press("enter")
            await pilot.pause(0.05)
            assert app.screen.query_one("#save", Button).disabled
            assert app.screen.query_one("#transport-ssh", Button).disabled
            assert app.screen.query_one("#ssh-host", Input).disabled
            await pilot.pause(0.6)
            assert not app.screen.query_one("#save", Button).disabled
            assert not app.screen.query_one("#ssh-host", Input).disabled

    asyncio.run(scenario())


@needs_pinned_textual
def test_settings_warns_about_all_relevant_env_overrides(monkeypatch, tmp_path, probe, http_probe):
    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    monkeypatch.setenv("RAIDFORGE_REPORT_TRANSPORT", "http")
    monkeypatch.setenv("RAIDFORGE_REPORT_HTTP_PORT", "9090")
    ctx = _context(tmp_path, config_dir)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("s")
            await pilot.pause(0.3)
            note = str(app.screen._note.content)
            assert "RAIDFORGE_REPORT_TRANSPORT" in note
            assert "RAIDFORGE_REPORT_HTTP_PORT" in note

    asyncio.run(scenario())
