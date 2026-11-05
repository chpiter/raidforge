from __future__ import annotations

import threading
from types import SimpleNamespace

from raidforge.agent.models import RemoteTask, RemoteTaskType
from raidforge.agent.queue import RemoteAdmission, coalesce_key
from raidforge.ui.models import TaskKind, TaskStatus
from raidforge.ui.tasks import TaskRunner, make_task

from agent_fakes import wait_for


class _FakeRunner:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, task, job, *, priority=0, origin="local"):
        task.priority = priority
        task.origin = origin
        self.submitted.append(task)
        return task


def _state():
    return SimpleNamespace(
        task_queue=[],
        active_task_id=None,
        cancel_event=threading.Event(),
        refresh=SimpleNamespace(generation=0),
    )


def _task(task_id="tsk_01JB8Z4Q2M", lease="lse_7f3c9a1b", task_type=RemoteTaskType.LIST_VDS, params=None):
    return RemoteTask(
        task_id=task_id,
        type=task_type,
        server_sn="SN",
        params=params or {},
        lease_id=lease,
    )


def test_task_runner_continues_after_job_exception():
    state = _state()
    runner = TaskRunner(state, app=None, broadcast=lambda message: None, services=None)
    seen = []

    def boom(ctx):
        seen.append("boom")
        raise RuntimeError("fail")

    def ok(ctx):
        seen.append("ok")
        return TaskStatus.SUCCEEDED

    first = make_task(TaskKind.REFRESH, "boom")
    second = make_task(TaskKind.REFRESH, "ok")
    runner.submit(first, boom)
    runner.submit(second, ok)
    assert wait_for(lambda: len(seen) == 2)
    assert first.status is TaskStatus.FAILED
    assert second.status is TaskStatus.SUCCEEDED
    runner.shutdown()


def test_local_priority_preempts_remote():
    state = _state()
    runner = TaskRunner(state, app=None, broadcast=lambda message: None, services=None)
    gate = threading.Event()
    order = []

    def blocking(ctx):
        gate.wait(2.0)
        order.append("blocking")
        return TaskStatus.SUCCEEDED

    def local(ctx):
        order.append("local")
        return TaskStatus.SUCCEEDED

    def remote(ctx):
        order.append("remote")
        return TaskStatus.SUCCEEDED

    runner.submit(make_task(TaskKind.REFRESH, "blocking"), blocking)
    runner.submit(make_task(TaskKind.REFRESH, "remote"), remote, priority=100, origin="remote")
    runner.submit(make_task(TaskKind.REFRESH, "local"), local, priority=0, origin="local")
    gate.set()
    assert wait_for(lambda: len(order) == 3)
    assert order == ["blocking", "local", "remote"]
    runner.shutdown()


def test_admission_coalesces_read_only_and_marks_old_skipped():
    runner = _FakeRunner()
    admission = RemoteAdmission(runner, lambda task: make_task(TaskKind.REMOTE, task.task_id))
    first = _task("tsk_00000001", "lse_00000001")
    second = _task("tsk_00000002", "lse_00000002")
    assert coalesce_key(first) == coalesce_key(second)
    assert admission.submit_remote(first, object()) == "accepted"
    assert admission.submit_remote(second, object()).startswith("coalesced:")
    assert runner.submitted[0].skip_dispatch is True
    assert len(runner.submitted) == 2


def test_admission_limits_remote_and_destructive():
    runner = _FakeRunner()
    admission = RemoteAdmission(runner, lambda task: make_task(TaskKind.REMOTE, task.task_id))
    for index, task_type in enumerate(
        (RemoteTaskType.LIST_PDS, RemoteTaskType.LIST_VDS, RemoteTaskType.LIST_CONTROLLERS)
    ):
        task = _task(f"tsk_0000000{index}", f"lse_0000000{index}", task_type)
        assert admission.can_accept(task)
        assert admission.submit_remote(task, object()) == "accepted"
    assert admission.can_accept(_task("tsk_00000009", "lse_00000009", RemoteTaskType.LIST_PDS)) is False

    other = RemoteAdmission(_FakeRunner(), lambda task: make_task(TaskKind.REMOTE, task.task_id))
    create = _task("tsk_00000010", "lse_00000010", RemoteTaskType.CREATE_RAID)
    create2 = _task("tsk_00000011", "lse_00000011", RemoteTaskType.CREATE_RAID)
    assert other.submit_remote(create, object()) == "accepted"
    assert other.can_accept(create2) is False
