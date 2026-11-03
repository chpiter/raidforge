"""Fidelity regression: QEMU-shaped inventory, exact command, manifest counters.

The shipped QEMU rehearsal always looks synthetic (``QEMU NVMe Ctrl`` /
``ERASE01``); these tests prove the report reproduces that data faithfully and
does not invent RAID state, rounds or test-fixture values.
"""

from __future__ import annotations

import pytest

pytest.importorskip("docx", reason="needs python-docx to build the report")
pytest.importorskip("PIL", reason="needs Pillow to embed screenshots")

from docx import Document  # noqa: E402

from raidforge.config import load_config  # noqa: E402
from raidforge.core.erase_report import build_erase_report  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.models import (  # noqa: E402
    CommandPlan,
    DetectionResult,
    DiskInfo,
    EraseRun,
    Medium,
    Protocol,
    SubmitResult,
    SubmitStatus,
    Vendor,
)
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.main import finalize_session  # noqa: E402
from raidforge.ui.state import AppContext  # noqa: E402

SESSION_START = "2026-11-03T02:34:47Z"

_REMOVED = (
    "SN 来源",
    "主机名",
    "操作模式",
    "擦除轮次",
    "第 1 次",
    "第 2 次",
    "第 1 轮",
    "第 2 轮",
    "实擦",
    "干跑",
)


def _qemu_disk() -> DiskInfo:
    return DiskInfo(
        disk_id="nvme0n1",
        controller_index=0,
        vendor=Vendor.GENERIC,
        medium=Medium.SSD,
        protocol=Protocol.NVME,
        model="QEMU NVMe Ctrl",
        serial="ERASE01",
        firmware="7.2.22",
        size_bytes=2147483648,
        device_node="/dev/nvme0n1",
    )


def _untouched_disk() -> DiskInfo:
    return DiskInfo(
        disk_id="nvme1n1",
        controller_index=0,
        vendor=Vendor.GENERIC,
        medium=Medium.SSD,
        protocol=Protocol.NVME,
        model="REAL NVMe",
        serial="OTHER001",
        firmware="1.0",
        size_bytes=960197124096,
        device_node="/dev/nvme1n1",
    )


def _run(disk: DiskInfo) -> EraseRun:
    plan = CommandPlan(
        disk_id=disk.disk_id,
        kind="ssd_sanitize",
        label="SSD sanitize",
        argv=["nvme", "format", "/dev/nvme0n1", "--force", "--ses=2"],
        medium=disk.medium,
        vendor=Vendor.GENERIC,
    )
    result = SubmitResult(
        disk_id=disk.disk_id,
        kind=plan.kind,
        status=SubmitStatus.ASYNC_SUBMITTED,
        return_code=0,
        duration_sec=0.134,
        stdout="Success formatting namespace:1",
        argv=list(plan.argv),
    )
    return EraseRun(
        index=1,
        run_epoch=1,
        started_at="2026-11-03T02:36:35Z",
        finished_at="2026-11-03T02:36:36Z",
        dry_run=False,
        cancelled=False,
        plans=[plan],
        results=[result],
        disks={disk.disk_id: disk},
    )


def _report_text(document) -> str:
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def test_qemu_shaped_report_is_faithful(tmp_path):
    out_dir = tmp_path / "session"
    out_dir.mkdir()
    disk = _qemu_disk()
    untouched = _untouched_disk()
    report = build_erase_report(
        server_sn="UNK605419DAFDBF",
        session_id="UNK605419DAFDBF_20261103T023447Z",
        session_started_at=SESSION_START,
        out_dir=out_dir,
        runs=[_run(disk)],
        snapshots=[],
        inventory=[disk, untouched],
        app_version="1.0.0",
        tool="sg3_utils",
    )
    assert report is not None

    text = _report_text(Document(str(report)))
    assert "QEMU NVMe Ctrl" in text
    assert "ERASE01" in text and "2.00 GB" in text
    assert "FW 7.2.22" in text
    assert "RAID 7.2.22" not in text
    assert "nvme format /dev/nvme0n1 --force --ses=2" in text
    assert "Success formatting namespace:1" in text
    assert "已选中" in text
    assert "OTHER001" in text and "未选中" in text
    for removed in _REMOVED:
        assert removed not in text, f"removed field still present: {removed}"


def _context(tmp_path) -> AppContext:
    cfg = load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {"enabled": False, "wait_for_route_seconds": 0},
            "tools": {"command_timeout": 5},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "UNK605419DAFDBF"})
    ev = EvidenceSession(cfg, sid.serial, session_id="UNK605419DAFDBF_20261103T023447Z")
    return AppContext(cfg=cfg, evidence=ev, server_id=sid)


def test_manifest_counters_and_parent_inheritance(tmp_path):
    disk = _qemu_disk()
    untouched = _untouched_disk()
    ctx = _context(tmp_path)
    ctx.inventory = [disk, untouched]
    ctx.erase_history = [_run(disk)]
    ctx.detection = DetectionResult(tool="sg3_utils", vendor=Vendor.GENERIC, controllers=[])

    manifest = finalize_session(ctx)
    assert manifest["tool"] == "sg3_utils"
    assert manifest["disk_count"] == 2
    assert len(manifest["inventory"]) == 2
    assert len(manifest["submissions"]) == 1
    assert manifest["submissions"][0]["disk_id"] == "nvme0n1"
    assert manifest["erase_runs"][0]["disks"][0]["firmware"] == "7.2.22"
    assert manifest["erase_runs"][0]["attempts"][0]["planned"] is True

    parent = _context(tmp_path)
    parent_manifest = finalize_session(parent)
    for key in ("tool", "disk_count", "inventory", "submissions"):
        assert parent_manifest[key] == manifest[key], key
    assert parent_manifest["erase_runs"] == manifest["erase_runs"]
    assert parent_manifest["report_file"] == manifest["report_file"]
