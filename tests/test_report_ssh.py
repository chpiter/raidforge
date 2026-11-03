"""The settings-page SSH probe: key login + remote root writability.

The probe exists because a wrong IP used to surface only after a session
finished, as "upload failed, queued locally".  These tests pin the argv (so the
short ConnectTimeout cannot silently disappear into rsync's ``-e`` string) and
the guard order (misconfiguration must not spawn ssh at all).
"""

from __future__ import annotations

from raidforge.config import Config
from raidforge.core import report
from raidforge.core.utils import RC_NOT_FOUND, CmdResult


def _cfg(tmp_path) -> Config:
    key = tmp_path / "evidence_ed25519"
    key.write_text("key\n", encoding="utf-8")
    cfg = Config()
    cfg.report.ssh_host = "192.168.40.129"
    cfg.report.ssh_user = "evidence"
    cfg.report.ssh_port = 22
    cfg.report.ssh_key_path = str(key)
    cfg.report.ssh_remote_root = "/srv/evidence/incoming"
    return cfg


def _result(return_code: int = 0, stdout: str = "", stderr: str = "") -> CmdResult:
    return CmdResult(return_code, stdout, stderr, 0.0, ["ssh"])


def _install(monkeypatch, result: CmdResult):
    """Pretend rsync exists and record every run_cmd call."""

    calls = []

    def fake(argv, timeout=60):
        calls.append((argv, timeout))
        return result

    monkeypatch.setattr(report.shutil, "which", lambda name: "/usr/bin/rsync")
    monkeypatch.setattr(report, "run_cmd", fake)
    return calls


def test_success_builds_expected_argv(tmp_path, monkeypatch):
    calls = _install(monkeypatch, _result(0))
    cfg = _cfg(tmp_path)

    check = report.check_ssh(cfg, timeout=5.0)

    assert check.ok is True
    assert "reachable" in check.message
    assert len(calls) == 1
    argv, timeout = calls[0]
    assert argv[0] == "ssh"
    assert ["-p", "22"] == argv[1:3]
    assert ["-i", cfg.report.ssh_key_path] == argv[3:5]
    assert "ConnectTimeout=5" in argv
    assert "ConnectionAttempts=1" in argv
    # Last two arguments: destination then the remote command.
    assert argv[-2] == "evidence@192.168.40.129"
    assert argv[-1] == "test -d /srv/evidence/incoming && test -w /srv/evidence/incoming"
    assert timeout >= 5.0


def test_empty_host_does_not_spawn_ssh(tmp_path, monkeypatch):
    calls = _install(monkeypatch, _result(0))
    cfg = _cfg(tmp_path)
    cfg.report.ssh_host = ""

    check = report.check_ssh(cfg)

    assert check.ok is False
    assert "no receiver IP" in check.message
    assert calls == []


def test_missing_key_does_not_spawn_ssh(tmp_path, monkeypatch):
    calls = _install(monkeypatch, _result(0))
    cfg = _cfg(tmp_path)
    cfg.report.ssh_key_path = str(tmp_path / "absent")

    check = report.check_ssh(cfg)

    assert check.ok is False
    assert "ssh key missing" in check.message
    assert calls == []


def test_missing_rsync_does_not_spawn_ssh(tmp_path, monkeypatch):
    calls = []

    def fake(argv, timeout=60):  # pragma: no cover - must never run
        calls.append(argv)
        return _result(0)

    monkeypatch.setattr(report.shutil, "which", lambda name: None)
    monkeypatch.setattr(report, "run_cmd", fake)

    check = report.check_ssh(_cfg(tmp_path))

    assert check.ok is False
    assert "rsync" in check.message
    assert calls == []


def test_permission_denied_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: "/usr/bin/rsync")
    monkeypatch.setattr(
        report,
        "run_cmd",
        lambda argv, timeout=60: _result(
            255, stderr="evidence@192.168.40.129: Permission denied (publickey).\n"
        ),
    )

    check = report.check_ssh(_cfg(tmp_path))

    assert check.ok is False
    assert "permission denied" in check.message.lower()
    assert "Permission denied" in check.detail


def test_unwritable_remote_root_is_reported(tmp_path, monkeypatch):
    _install(monkeypatch, _result(1))
    check = report.check_ssh(_cfg(tmp_path))

    assert check.ok is False
    assert "remote root" in check.message


def test_missing_ssh_client_is_reported(tmp_path, monkeypatch):
    _install(monkeypatch, _result(RC_NOT_FOUND))
    check = report.check_ssh(_cfg(tmp_path))

    assert check.ok is False
    assert "ssh client" in check.message


def test_rsync_command_has_no_connect_timeout(tmp_path):
    """A 5s ConnectTimeout inside rsync's -e would kill slow uploads."""

    assert "ConnectTimeout" not in report._ssh_command(_cfg(tmp_path))
