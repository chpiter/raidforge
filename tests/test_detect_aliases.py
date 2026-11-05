"""Detection ladder aliases: storcli64/perccli64/hpssacli and forced tools."""

from __future__ import annotations

from pathlib import Path

from raidforge.config import default_config
from raidforge.core import detect as detect_mod
from raidforge.core import enum_disks as enum_mod
from raidforge.core.models import Vendor
from raidforge.core.utils import CmdResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeShell:
    def __init__(self, table):
        self.table = table

    def __call__(self, cmd, timeout=60.0, **kwargs):
        argv = list(cmd) if not isinstance(cmd, str) else cmd.split()
        key = " ".join(argv)
        if key in self.table:
            return CmdResult(0, self.table[key], "", 0.01, argv)
        return CmdResult(127, "", f"no such file or directory: {key}", 0.001, argv)


def install(monkeypatch, table, available):
    monkeypatch.setattr(detect_mod, "run_cmd", FakeShell(table))
    monkeypatch.setattr(detect_mod, "which", lambda name: f"/usr/local/bin/{name}" if name in available else None)
    monkeypatch.setattr(enum_mod, "run_cmd", FakeShell(table))


STOR_CLI_TABLE = {
    "storcli show J": text("storcli_show_J.json"),
    "storcli /c0 show J": text("storcli_c0_show_J.json"),
    "storcli /c0/eall/sall show J": text("storcli_sall_show_J.json"),
}
STORCLI64_TABLE = {key.replace("storcli", "storcli64"): value for key, value in STOR_CLI_TABLE.items()}
PERCCLI64_TABLE = {key.replace("storcli", "perccli64"): value for key, value in STOR_CLI_TABLE.items()}


def test_storcli64_alias(monkeypatch):
    cfg = default_config()
    install(monkeypatch, STORCLI64_TABLE, {"storcli64"})
    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "storcli"
    assert detection.binary == "storcli64"
    assert detection.controllers[0].vendor is Vendor.LSI
    disks, raw = enum_mod.enumerate_disks(cfg, detection)
    assert len(disks) == 3
    assert raw[0][0] == "lsi_c0_sall_show_J.json"


def test_perccli64_alias(monkeypatch):
    cfg = default_config()
    install(monkeypatch, PERCCLI64_TABLE, {"perccli64"})
    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "perccli"
    assert detection.binary == "perccli64"
    assert detection.provider == "lsi"
    assert detection.controllers[0].vendor is Vendor.LSI
    assert any(attempt.binary == "perccli64" for attempt in detection.attempts if attempt.ok)


def test_hpssacli_alias(monkeypatch):
    cfg = default_config()
    install(
        monkeypatch,
        {"hpssacli ctrl all show": text("ssacli_ctrl_all_show.txt")},
        {"hpssacli"},
    )
    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "ssacli"
    assert detection.binary == "hpssacli"
    assert detection.controllers[0].vendor is Vendor.HPE
    assert detection.controllers[0].binary == "hpssacli"


def test_forced_tool_is_normalised(monkeypatch):
    cfg = default_config()
    cfg.tools.forced_tool = "hpssacli"
    install(
        monkeypatch,
        {
            "ssacli ctrl all show": text("ssacli_ctrl_all_show.txt"),
            **STOR_CLI_TABLE,
        },
        {"ssacli", "storcli"},
    )
    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "ssacli"
    assert detection.binary == "ssacli"


def test_detection_order_alias(monkeypatch):
    cfg = default_config()
    cfg.tools.detection_order = ["storcli64"]
    install(monkeypatch, STORCLI64_TABLE, {"storcli64"})
    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "storcli"
    assert detection.binary == "storcli64"


HIRA_TABLE = {
    "hiraidadm show allctrl": (FIXTURES / "hiraidadm" / "show_allctrl.txt").read_text(encoding="utf-8"),
    "hiraidadm c0 show": (FIXTURES / "hiraidadm" / "c0_show.txt").read_text(encoding="utf-8"),
}


def test_hiraidadm_wins_over_storcli(monkeypatch):
    cfg = default_config()
    install(monkeypatch, {**HIRA_TABLE, **STOR_CLI_TABLE}, {"hiraidadm", "storcli"})
    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "hiraidadm"
    assert detection.binary == "hiraidadm"
    assert detection.provider == "huawei"
    assert detection.controllers[0].vendor is Vendor.HUAWEI
    assert detection.controllers[0].serial == "2102312XYZ"
    assert detection.controllers[0].model == "SP686C-M-16i"


def test_hiraidadm_without_controllers_falls_through_to_storcli(monkeypatch):
    cfg = default_config()
    table = {
        "hiraidadm show allctrl": "Controllers Number = 0\n",
        **STOR_CLI_TABLE,
    }
    install(monkeypatch, table, {"hiraidadm", "storcli"})
    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "storcli"
    assert detection.binary == "storcli"


def test_forced_hiraid_alias(monkeypatch):
    cfg = default_config()
    cfg.tools.forced_tool = "hiraid"
    install(monkeypatch, {**HIRA_TABLE, **STOR_CLI_TABLE}, {"hiraidadm", "storcli"})
    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "hiraidadm"
    assert detection.binary == "hiraidadm"
