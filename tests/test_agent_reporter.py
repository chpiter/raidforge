from __future__ import annotations

from pathlib import Path

from raidforge.agent.client import Response
from raidforge.agent.evidence import TaskEvidence
from raidforge.agent.evidence_uploader import EvidenceUploader
from raidforge.agent.reporter import ResultReporter
from raidforge.agent.state import StateStore

from agent_fakes import FakeClient, FakeClock


def _payload():
    return {
        "task_id": "tsk_01JB8Z4Q2M",
        "lease_id": "lse_7f3c9a1b",
        "status": "finished",
        "evidence": {"status": "pending", "manifest": []},
    }


def test_result_success_drops_pending(tmp_path: Path):
    store = StateStore(tmp_path / "state", clock=FakeClock())
    client = FakeClient()
    client.push("result", Response(status=200, body={"ok": True}))
    reporter = ResultReporter(client, store, FakeClock())
    assert reporter.deliver(_payload()) is True
    assert list(store.iter_pending_results()) == []
    assert store.load_result_copy("tsk_01JB8Z4Q2M") is not None


def test_result_transient_keeps_pending_and_retries(tmp_path: Path):
    clock = FakeClock()
    store = StateStore(tmp_path / "state", clock=clock)
    client = FakeClient()
    client.push("result", Response(status=500, text="boom"))
    client.push("result", Response(status=500, text="boom"))
    client.push("result", Response(status=200, body={"ok": True}))
    reporter = ResultReporter(client, store, clock)
    reporter.deliver(_payload())
    assert len(list(store.iter_pending_results())) == 1
    assert reporter.drain_once() == 0  # 30s backoff was scheduled by deliver()
    clock.advance(31)
    assert reporter.drain_once() == 0  # second failure schedules 60s
    clock.advance(61)
    assert reporter.drain_once() == 1
    assert list(store.iter_pending_results()) == []


def test_result_gone_is_dropped(tmp_path: Path):
    store = StateStore(tmp_path / "state", clock=FakeClock())
    client = FakeClient()
    client.push("result", Response(status=410, body={"error_code": "task_gone"}))
    reporter = ResultReporter(client, store, FakeClock())
    assert reporter.deliver(_payload()) is True
    assert list(store.iter_pending_results()) == []


def test_evidence_upload_complete(tmp_path: Path):
    store = StateStore(tmp_path / "state", clock=FakeClock())
    client = FakeClient()
    client.push("evidence", Response(status=200, body={"ok": True, "evidence_id": "e1"}))
    reporter = ResultReporter(client, store, FakeClock())
    uploader = EvidenceUploader(client, store, tmp_path / "evidence", reporter=reporter, clock=FakeClock())
    evidence = TaskEvidence(tmp_path / "evidence", "tsk_01JB8Z4Q2M", clock=FakeClock())
    record = evidence.write_json("cmd-001.json", {"ok": True}, kind="command_output", stage="execute")
    status = uploader.upload_task("tsk_01JB8Z4Q2M", "lse_7f3c9a1b", evidence, manifest=[record.to_dict()])
    assert status == "complete"
    assert list(store.iter_pending_evidence()) == []


def test_evidence_exists_is_success(tmp_path: Path):
    store = StateStore(tmp_path / "state", clock=FakeClock())
    client = FakeClient()
    client.push("evidence", Response(status=409, body={"error_code": "evidence_exists"}))
    uploader = EvidenceUploader(client, store, tmp_path / "evidence", clock=FakeClock())
    evidence = TaskEvidence(tmp_path / "evidence", "tsk_01JB8Z4Q2M", clock=FakeClock())
    record = evidence.write_json("cmd-001.json", {}, kind="command_output", stage="execute")
    assert uploader.upload_task("tsk_01JB8Z4Q2M", "lse_7f3c9a1b", evidence, manifest=[record.to_dict()]) == "complete"


def test_evidence_retries_then_stops_partial(tmp_path: Path):
    clock = FakeClock()
    store = StateStore(tmp_path / "state", clock=clock)
    client = FakeClient()
    for _ in range(12):
        client.push("evidence", Response(status=413, body={"error_code": "too_large"}))
    uploader = EvidenceUploader(client, store, tmp_path / "evidence", clock=clock)
    evidence = TaskEvidence(tmp_path / "evidence", "tsk_01JB8Z4Q2M", clock=clock)
    record = evidence.write_json("cmd-001.json", {}, kind="command_output", stage="execute")
    assert uploader.upload_task("tsk_01JB8Z4Q2M", "lse_7f3c9a1b", evidence, manifest=[record.to_dict()]) == "partial"
    assert list(store.iter_pending_evidence()) == []
