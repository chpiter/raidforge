"""Network readiness: any NIC with a global IPv4 counts as "up".

Field servers have several ports and may sit on an L2 segment without a
gateway, so a default-route-only check used to report "down" while rsync to the
collector in the same subnet would have worked.
"""

from __future__ import annotations

from raidforge.core import report
from raidforge.core.utils import CmdResult


def _result(stdout: str, return_code: int = 0) -> CmdResult:
    return CmdResult(return_code, stdout, "", 0.0, ["ip"])


ADDR_LINES = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
    "2: eno1    inet 192.168.40.50/24 brd 192.168.40.255 scope global eno1\\       valid_lft 3600sec\n"
    "3: eno2    inet 10.0.0.7/24 brd 10.0.0.255 scope global eno2\\       valid_lft 3600sec\n"
)


def test_global_ipv4_addresses_parses_and_skips_loopback(monkeypatch):
    monkeypatch.setattr(report, "run_cmd", lambda argv, timeout=5: _result(ADDR_LINES))
    assert report.global_ipv4_addresses() == [
        ("eno1", "192.168.40.50/24"),
        ("eno2", "10.0.0.7/24"),
    ]
    assert report.network_summary() == "eno1 192.168.40.50"


def test_global_ipv4_addresses_handles_command_failure(monkeypatch):
    monkeypatch.setattr(report, "run_cmd", lambda argv, timeout=5: _result("", return_code=1))
    assert report.global_ipv4_addresses() == []
    assert report.network_summary() == ""


def test_lease_without_default_route_is_ready(monkeypatch):
    def fake(argv, timeout=5):
        if "route" in argv:
            return _result("")
        return _result('2: ens3    inet 10.20.30.40/24 scope global ens3\\       valid_lft 1sec\n')

    monkeypatch.setattr(report, "run_cmd", fake)
    ready, _waited = report.wait_for_default_route(timeout=0)
    assert ready is True


def test_nothing_up_is_not_ready(monkeypatch):
    monkeypatch.setattr(report, "run_cmd", lambda argv, timeout=5: _result(""))
    ready, waited = report.wait_for_default_route(timeout=0)
    assert ready is False
    assert waited >= 0
