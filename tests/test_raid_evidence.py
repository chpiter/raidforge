"""RAID audit artifacts: raid_actions.json and manifest metadata."""

from __future__ import annotations

from raidforge.config import default_config
from raidforge.core.evidence import EvidenceSession
from raidforge.core.models import (
    RaidAction,
    RaidActionRun,
    RaidActionResult,
    RaidCommandPlan,
    RaidStatus,
    Vendor,
)
from raidforge.core.publish import _raid_summary


def build_run() -> RaidActionRun:
    plan = RaidCommandPlan(
        action=RaidAction.CREATE,
        controller_index=0,
        vendor=Vendor.LSI,
        label="create RAID1",
        argv=["storcli", "/c0", "add", "vd", "r1", "drives=32:0,32:1"],
        level="RAID1",
        disk_ids=["c0e32s0", "c0e32s1"],
    )
    result = RaidActionResult(
        action=RaidAction.CREATE,
        controller_index=0,
        status=RaidStatus.ACCEPTED,
        return_code=0,
        duration_sec=0.2,
        argv=list(plan.argv),
        label=plan.label,
        observed="confirmed",
        observed_detail="1/1 target volume visible",
    )
    return RaidActionRun(
        index=1,
        epoch=1,
        action=RaidAction.CREATE,
        started_at="2026-11-03T00:00:00Z",
        finished_at="2026-11-03T00:00:01Z",
        dry_run=False,
        cancelled=False,
        controller_index=0,
        plans=[plan],
        results=[result],
        topology_before={"volumes": []},
        topology_after={"volumes": [{"volume_id": "v0"}]},
    )


def test_raid_actions_json_is_hashed_by_the_manifest(tmp_path):
    cfg = default_config()
    cfg.evidence.root = str(tmp_path)
    evidence = EvidenceSession(cfg, "SN-RAID", session_id="SN-RAID_20261103T000000Z")
    run = build_run()
    evidence.write_json("raid_actions.json", [run.to_json_obj()])
    manifest = evidence.finalize({"raid_runs": _raid_summary([run])})

    assert manifest["schema"] == "raidforge-evidence/manifest@2"
    assert manifest["raid_runs"][0]["action"] == "create"
    assert manifest["raid_runs"][0]["observed"] == ["confirmed"]
    files = {item["path"]: item for item in manifest["files"]}
    assert "raid_actions.json" in files
    assert len(files["raid_actions.json"]["sha256"]) == 64


def test_raid_summary_skips_bulky_details():
    run = build_run()
    summary = _raid_summary([run])
    assert summary[0]["counts"]["accepted"] == 1
    assert "plans" not in summary[0]
    assert "results" not in summary[0]
