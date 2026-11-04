from __future__ import annotations

import json
from pathlib import Path

from raidforge.agent.state import StateStore

from agent_fakes import FakeClock


def test_state_roundtrip_and_atomic_file(tmp_path: Path):
    store = StateStore(tmp_path / "state", clock=FakeClock())
    store.set_identity("SN123", "rf-SN123")
    assert store.mark_executed("tsk_01JB8Z4Q2M", "lse_7f3c9a1b") is True
    assert store.mark_executed("tsk_01JB8Z4Q2M", "lse_7f3c9a1b") is False
    store.remember_task({"task_id": "tsk_01JB8Z4Q2M", "status": "finished"})
    store.set_paused(True, "terminal_state_unknown")

    reloaded = StateStore(tmp_path / "state", clock=FakeClock())
    state = reloaded.load()
    assert state.server_sn == "SN123"
    assert state.agent_id == "rf-SN123"
    assert "tsk_01JB8Z4Q2M:lse_7f3c9a1b" in state.executed_task_ids
    assert state.recent_tasks[-1]["status"] == "finished"
    assert reloaded.is_paused() == (True, "terminal_state_unknown")
    assert not list((tmp_path / "state").glob("*.tmp-*"))


def test_corrupt_state_is_quarantined(tmp_path: Path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "agent_state.json").write_text("{broken", encoding="utf-8")
    store = StateStore(root, clock=FakeClock())
    state = store.load()
    assert state.server_sn == ""
    assert list(root.glob("agent_state.corrupt-*.json"))


def test_pending_result_lifecycle(tmp_path: Path):
    store = StateStore(tmp_path / "state", clock=FakeClock())
    payload = {"task_id": "tsk_01JB8Z4Q2M", "lease_id": "lse_7f3c9a1b", "status": "finished"}
    path = store.put_pending_result(payload)
    assert path.exists()
    assert [item for _, item in store.iter_pending_results()][0]["status"] == "finished"
    store.drop_pending_result("tsk_01JB8Z4Q2M", "lse_7f3c9a1b")
    assert list(store.iter_pending_results()) == []
    assert store.load().executed_task_ids == []


def test_pending_evidence_lifecycle(tmp_path: Path):
    store = StateStore(tmp_path / "state", clock=FakeClock())
    store.put_pending_evidence(
        {"task_id": "tsk_01JB8Z4Q2M", "lease_id": "lse_7f3c9a1b", "manifest": [{"filename": "a.json"}]}
    )
    payload = next(iter(store.iter_pending_evidence()))[1]
    assert payload["manifest"][0]["filename"] == "a.json"
    store.drop_pending_evidence("tsk_01JB8Z4Q2M")
    assert list(store.iter_pending_evidence()) == []


def test_result_copy_retained_for_duplicate_delivery(tmp_path: Path):
    store = StateStore(tmp_path / "state", clock=FakeClock())
    store.save_result_copy({"task_id": "tsk_01JB8Z4Q2M", "status": "finished"})
    assert store.load_result_copy("tsk_01JB8Z4Q2M")["status"] == "finished"
    assert json.loads((tmp_path / "state" / "results" / "result_tsk_01JB8Z4Q2M.json").read_text())["status"] == "finished"
