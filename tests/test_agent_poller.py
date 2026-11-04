from __future__ import annotations

from types import SimpleNamespace

from raidforge.agent.client import Response
from raidforge.agent.poller import TaskPoller

from agent_fakes import FakeAdmission, FakeClient, FakeClock, FakeStore, make_envelope


class _Reporter:
    def __init__(self) -> None:
        self.delivered = []

    def deliver(self, payload):
        self.delivered.append(payload)
        return True


class _Runtime:
    def __init__(self) -> None:
        self.agent_cfg = SimpleNamespace(poll_interval_short=1, poll_jitter_ms=0)
        self.server_sn = "SN12345678"
        self.paused = False
        self.admission = FakeAdmission()
        self.store = FakeStore()
        self.reporter = _Reporter()
        self.rejections = []
        self.released = []
        self.aborts = []
        self.superseded = []

    def can_poll(self):
        return True

    def remote_job(self, task):
        return object()

    def report_rejection(self, envelope, rejection):
        self.rejections.append((envelope, rejection))

    def release_task(self, task, reason=""):
        self.released.append((task.task_id, reason))

    def handle_abort_current(self, task):
        self.aborts.append(task)
        return None

    def supersede(self, task_id):
        self.superseded.append(task_id)


def _poller(runtime, responses):
    client = FakeClient()
    for response in responses:
        client.push("next_task", response)
    return TaskPoller(client, runtime.admission, runtime.store, runtime, FakeClock()), client


def test_204_does_nothing():
    runtime = _Runtime()
    poller, _client = _poller(runtime, [Response(status=204)])
    assert poller.poll_once() == 0
    assert runtime.admission.submitted == []


def test_direct_task_envelope_is_accepted():
    runtime = _Runtime()
    poller, _client = _poller(runtime, [Response(status=200, body=make_envelope())])
    assert poller.poll_once() == 1
    assert len(runtime.admission.submitted) == 1


def test_valid_task_admitted():
    runtime = _Runtime()
    poller, _client = _poller(runtime, [Response(status=200, body={"ok": True, "task": make_envelope()})])
    assert poller.poll_once() == 1
    assert len(runtime.admission.submitted) == 1


def test_server_sn_mismatch_is_reported_without_execution():
    runtime = _Runtime()
    poller, _client = _poller(
        runtime,
        [Response(status=200, body={"ok": True, "task": make_envelope(server_sn="OTHER")})],
    )
    assert poller.poll_once() == 0
    assert runtime.admission.submitted == []
    assert runtime.rejections[0][1].error_code == "server_sn_mismatch"


def test_expired_lease_is_released():
    runtime = _Runtime()
    poller, client = _poller(
        runtime,
        [Response(status=200, body={"ok": True, "task": make_envelope(lease_expires_at="2020-01-01T00:00:00Z")})],
    )
    assert poller.poll_once() == 0
    assert any(call[0] == "release" for call in client.calls)


def test_abort_current_is_handled_directly():
    runtime = _Runtime()
    envelope = make_envelope(task_type="abort_current", params={"reason": "stop"})
    poller, _client = _poller(runtime, [Response(status=200, body={"ok": True, "task": envelope})])
    assert poller.poll_once() == 0
    assert len(runtime.aborts) == 1
    assert runtime.admission.submitted == []


def test_busy_admission_releases_lease():
    runtime = _Runtime()
    runtime.admission.accept = False
    poller, _client = _poller(runtime, [Response(status=200, body={"ok": True, "task": make_envelope()})])
    assert poller.poll_once() == 0
    assert runtime.released and runtime.released[0][1] == "busy"


def test_unknown_type_reported():
    runtime = _Runtime()
    poller, _client = _poller(
        runtime, [Response(status=200, body={"ok": True, "task": make_envelope(task_type="locate_led")})]
    )
    assert poller.poll_once() == 0
    assert runtime.rejections[0][1].error_code == "unsupported_task_type"
