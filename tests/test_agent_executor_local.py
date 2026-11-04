from __future__ import annotations

import threading
from types import SimpleNamespace

from raidforge.ui.models import TaskKind, TaskStatus
from raidforge.ui.tasks import TaskRunner, make_task

from agent_fakes import wait_for


def _state():
    return SimpleNamespace(
        task_queue=[],
        active_task_id=None,
        cancel_event=threading.Event(),
        refresh=SimpleNamespace(generation=0),
    )


def test_serial_execution_and_abort_event():
    state = _state()
    runner = TaskRunner(state, app=None, broadcast=lambda message: None, services=None)
    gate = threading.Event()
    order = []

    def first(ctx):
        gate.wait(2.0)
        order.append("first")
        return TaskStatus.SUCCEEDED

    def second(ctx):
        order.append("second")
        return TaskStatus.SUCCEEDED

    first_task = make_task(TaskKind.ERASE, "first")
    second_task = make_task(TaskKind.REFRESH, "second")
    runner.submit(first_task, first)
    runner.submit(second_task, second)
    assert wait_for(lambda: state.active_task_id == first_task.id)
    assert order == []
    assert runner.abort_task(first_task.id) is True
    assert first_task.abort_requested.is_set()
    gate.set()
    assert wait_for(lambda: len(order) == 2)
    assert order == ["first", "second"]
    assert second_task.status is TaskStatus.SUCCEEDED
    runner.shutdown()
