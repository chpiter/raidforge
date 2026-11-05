"""Huawei HiRAID erase command templates."""

from __future__ import annotations

import pytest

from raidforge.core.commands import build_command
from raidforge.core.models import DiskInfo, Medium, Protocol, Vendor


def disk(medium: Medium = Medium.HDD) -> DiskInfo:
    return DiskInfo(
        disk_id="c0e0s2",
        controller_index=0,
        vendor=Vendor.HUAWEI,
        medium=medium,
        protocol=Protocol.SAS,
        enclosure=0,
        slot=2,
        size_bytes=1000,
    )


@pytest.mark.parametrize(
    ("medium", "method", "expected"),
    [
        (Medium.HDD, "thorough", ["start", "erase", "mode=thorough"]),
        (Medium.HDD, "overwrite", ["start", "erase", "mode=simple"]),
        (Medium.HDD, "erase", ["start", "erase", "mode=normal"]),
        (Medium.SSD, "blockerase", ["start", "sanitize", "mode=blockerase"]),
        (Medium.SSD, "sanitize", ["start", "sanitize", "mode=cryptoerase"]),
    ],
)
def test_huawei_erase_argv(medium: Medium, method: str, expected) -> None:
    plan = build_command(disk(medium), method)
    assert plan.argv == ["hiraidadm", "c0:e0:s2", *expected]


def test_huawei_hdd_does_not_offer_sanitize() -> None:
    from raidforge.core.commands import available_methods

    assert "sanitize" not in available_methods(disk(Medium.HDD))
    assert "blockerase" in available_methods(disk(Medium.SSD))
