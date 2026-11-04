from __future__ import annotations

from pathlib import Path

import pytest

from raidforge.agent.evidence import EvidenceLimitExceeded, TaskEvidence, gc_evidence

from agent_fakes import FakeClock


def test_manifest_fields_and_hash(tmp_path: Path):
    evidence = TaskEvidence(tmp_path, "tsk_01JB8Z4Q2M", clock=FakeClock())
    record = evidence.write_json("plan.json", {"a": 1}, kind="plan", stage="confirm")
    assert record.filename == "plan.json"
    assert len(record.sha256) == 64
    assert record.content_type == "application/json"
    payload = evidence.finalize()
    assert payload["schema"] == "raidforge-agent-evidence@1"
    assert payload["files"][0]["size"] > 0
    assert payload["files"][0]["collected_at"].endswith("Z")


def test_limits_and_extension_rules(tmp_path: Path):
    evidence = TaskEvidence(tmp_path, "tsk_01JB8Z4Q2M", clock=FakeClock(), max_files=1)
    evidence.write_json("one.json", {}, kind="json", stage="execute")
    with pytest.raises(EvidenceLimitExceeded):
        evidence.write_json("two.json", {}, kind="json", stage="execute")
    with pytest.raises(EvidenceLimitExceeded):
        TaskEvidence(tmp_path, "tsk_02", clock=FakeClock()).write_bytes(
            "bad.exe", b"x", kind="other", stage="execute"
        )
    with pytest.raises(EvidenceLimitExceeded):
        TaskEvidence(tmp_path, "tsk_03", clock=FakeClock()).write_json(
            "../escape.json", {}, kind="json", stage="execute"
        )


def test_magic_byte_checked(tmp_path: Path):
    evidence = TaskEvidence(tmp_path, "tsk_01JB8Z4Q2M", clock=FakeClock())
    with pytest.raises(EvidenceLimitExceeded):
        evidence.write_bytes("shot.png", b"not-a-png", kind="screenshot", stage="execute")


def test_events_are_fsynced_lines(tmp_path: Path):
    evidence = TaskEvidence(tmp_path, "tsk_01JB8Z4Q2M", clock=FakeClock())
    evidence.emit("started", phase="running")
    evidence.emit("finished")
    lines = evidence.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"event": "started"' in lines[0]


def test_gc_removes_only_old_task_dirs(tmp_path: Path):
    import os
    import time

    old = tmp_path / "tsk_old"
    old.mkdir()
    (old / "a.json").write_text("{}", encoding="utf-8")
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))
    new = tmp_path / "tsk_new"
    new.mkdir()
    removed = gc_evidence(tmp_path, retention_days=7)
    assert old in removed
    assert new.exists()
