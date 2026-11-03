"""Config precedence and the TUI-owned site overlay.

The TUI settings screen must be able to change the receiver IP for the current
boot, while the factory template stays factory-owned and environment variables
keep their documented "highest except --set" rank.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from raidforge import config as config_mod


def _isolate(monkeypatch, tmp_path: Path) -> Path:
    """Point every config layer at a private directory and clear RAIDFORGE_*."""

    config_dir = tmp_path / "config.d"
    monkeypatch.setattr(config_mod, "SYSTEM_CONFIG_PATH", str(tmp_path / "system.yaml"))
    monkeypatch.setattr(config_mod, "LOCAL_CONFIG_PATH", str(tmp_path / "local.yaml"))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(config_mod, "SITE_OVERRIDE_PATH", str(config_dir / "90-site.yaml"))
    for key in list(os.environ):
        if key.startswith(config_mod.ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    return config_dir


def test_config_d_overrides_template_and_env_wins(monkeypatch, tmp_path):
    config_dir = _isolate(monkeypatch, tmp_path)
    Path(config_mod.SYSTEM_CONFIG_PATH).write_text(
        "report:\n  ssh_host: 1.1.1.1\n  ssh_user: evidence\n", encoding="utf-8"
    )
    config_dir.mkdir()
    (config_dir / "50-site.yaml").write_text("report:\n  ssh_host: 2.2.2.2\n", encoding="utf-8")
    (config_dir / "90-site.yaml").write_text("report:\n  ssh_host: 3.3.3.3\n", encoding="utf-8")

    cfg = config_mod.load_config(config_dir=str(config_dir))
    assert cfg.report.ssh_host == "3.3.3.3", "highest-sorting fragment must win"

    monkeypatch.setenv("RAIDFORGE_REPORT_SSH_HOST", "4.4.4.4")
    cfg = config_mod.load_config(config_dir=str(config_dir))
    assert cfg.report.ssh_host == "4.4.4.4", "env must beat config.d"

    cfg = config_mod.load_config(
        config_dir=str(config_dir), overrides={"report": {"ssh_host": "5.5.5.5"}}
    )
    assert cfg.report.ssh_host == "5.5.5.5", "explicit overrides must beat env"


def test_write_site_override_merges_and_is_reloadable(monkeypatch, tmp_path):
    config_dir = _isolate(monkeypatch, tmp_path)
    config_dir.mkdir()
    existing = config_dir / "90-site.yaml"
    existing.write_text(
        "report:\n  http_base_url: http://10.0.0.5:8080\n  ssh_user: evidence\n",
        encoding="utf-8",
    )

    written = config_mod.write_site_override({"report": {"ssh_host": "10.1.2.3"}})
    assert written == existing

    cfg = config_mod.load_config(config_dir=str(config_dir))
    assert cfg.report.ssh_host == "10.1.2.3"
    assert cfg.report.http_base_url == "http://10.0.0.5:8080", "unrelated keys must survive"
    assert cfg.report.ssh_user == "evidence"

    if os.name == "posix":
        mode = stat.S_IMODE(existing.stat().st_mode)
        assert mode == 0o644, f"unexpected mode {oct(mode)}"
    assert not list(config_dir.glob(".90-site-*")), "temporary file left behind"


def test_write_site_override_creates_missing_directory(monkeypatch, tmp_path):
    config_dir = _isolate(monkeypatch, tmp_path)
    assert not config_dir.exists()

    config_mod.write_site_override({"report": {"ssh_host": "10.9.9.9"}})
    assert config_mod.load_config(config_dir=str(config_dir)).report.ssh_host == "10.9.9.9"


def test_report_transport_and_ports_default_and_normalise(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = config_mod.load_config(config_dir=str(tmp_path / "config.d"))
    assert cfg.report.transport == "ssh"
    assert cfg.report.ssh_port == 22
    assert cfg.report.http_port == 8080
    assert cfg.report.run_upload_seconds == 30.0

    cfg = config_mod.load_config(
        config_dir=str(tmp_path / "config.d"),
        overrides={
            "report": {
                "transport": "HTTP ",
                "ssh_port": "2222",
                "http_port": "9090",
            }
        },
    )
    assert cfg.report.transport == "http"
    assert cfg.report.ssh_port == 2222
    assert cfg.report.http_port == 9090


@pytest.mark.parametrize(
    "transport,expected",
    [("ssh", "ssh"), ("HTTP", "http"), ("", "ssh"), ("telnet", "ssh")],
)
def test_transport_normalisation(transport, expected):
    assert config_mod.normalize_transport(transport) == expected


@pytest.mark.parametrize("port,expected", [("8080", 8080), (2222, 2222), ("0", 22), ("70000", 22), ("abc", 22)])
def test_port_normalisation(port, expected):
    assert config_mod._coerce_port(port, 22, "ssh_port") == expected


def test_report_env_overrides_transport_and_ports(monkeypatch, tmp_path):
    config_dir = _isolate(monkeypatch, tmp_path)
    config_dir.mkdir()
    (config_dir / "50-site.yaml").write_text(
        "report:\n  transport: ssh\n  ssh_port: 22\n  http_port: 8080\n", encoding="utf-8"
    )
    monkeypatch.setenv("RAIDFORGE_REPORT_TRANSPORT", "http")
    monkeypatch.setenv("RAIDFORGE_REPORT_SSH_PORT", "2222")
    monkeypatch.setenv("RAIDFORGE_REPORT_HTTP_PORT", "9090")
    monkeypatch.setenv("RAIDFORGE_REPORT_RUN_UPLOAD_SECONDS", "7")

    cfg = config_mod.load_config(config_dir=str(config_dir))
    assert cfg.report.transport == "http"
    assert cfg.report.ssh_port == 2222
    assert cfg.report.http_port == 9090
    assert cfg.report.run_upload_seconds == 7.0
