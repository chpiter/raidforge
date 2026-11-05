"""远程任务执行：预检、确认、租约、写入、后校验与结果组装。"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import replace
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import (
    RaidActionResult,
    RaidStatus,
    utc_now_iso,
)
from ..core.raid.execute import execute_raid_plans
from . import adapters, safety
from .clock import parse_rfc3339, rfc3339
from .evidence import EvidenceLimitExceeded, TaskEvidence
from .models import (
    AgentResult,
    CommandRecord,
    ConfirmDecision,
    ConfirmRequest,
    RemoteTask,
    RemoteTaskType,
    ReportedStatus,
    TaskRejected,
)
from .snapshot import HostSnapshot
from .topology import topology_hash
from .validation import validate_envelope

log = logging.getLogger(__name__)


def build_remote_job(runtime: Any, task: RemoteTask) -> Any:
    """TaskRunner Job：返回 UI TaskStatus。"""

    def job(ctx: Any):
        from ..ui.models import TaskStatus

        if runtime.store.is_executed(task.task_id, task.lease_id):
            copy = runtime.store.load_result_copy(task.task_id)
            if copy:
                runtime.reporter.deliver(copy)
            runtime.admission.mark_finished(task.task_id)
            ctx.log("duplicate delivery: result re-sent, no physical command")
            return TaskStatus.SUCCEEDED

        runtime.admission.mark_running(task.task_id)
        runtime.on_task_running(task)
        try:
            result = execute(ctx, runtime, task)
        except Exception as exc:  # 执行器异常不得让队列退出
            log.exception("远程任务异常 %s", task.task_id)
            result = runtime.internal_error_result(task, exc)
            runtime.pause_after_failure(task, str(exc))

        if result is None:
            runtime.admission.mark_finished(task.task_id)
            return TaskStatus.SUCCEEDED

        finished_status = result.status
        runtime.finish_remote(task, result)
        if finished_status is ReportedStatus.FINISHED:
            return TaskStatus.SUCCEEDED
        if result.error_code == "cancelled":
            return TaskStatus.CANCELLED
        return TaskStatus.FAILED

    return job


def execute(ctx: Any, runtime: Any, task: RemoteTask) -> Optional[AgentResult]:
    result = AgentResult(
        task_id=task.task_id,
        agent_id=runtime.agent_cfg.agent_id,
        lease_id=task.lease_id,
        started_at=rfc3339(runtime.clock.utcnow()),
    )
    try:
        validate_envelope(task, runtime.server_sn, runtime.clock.utcnow())
    except TaskRejected as rejection:
        return _fail(runtime, result, rejection)

    if task.type is RemoteTaskType.ABORT_CURRENT:
        return runtime.handle_abort_current(task)

    try:
        snapshot = runtime.capture_snapshot(fresh=True)
    except Exception as exc:
        log.exception("扫描失败")
        result.fail("internal_error", f"扫描失败: {exc}")
        return _finish(runtime, result)

    if task.read_only:
        return _run_read(ctx, runtime, task, snapshot, result)

    return _run_destructive(ctx, runtime, task, snapshot, result)


# ---------------------------------------------------------------------------
# read-only
# ---------------------------------------------------------------------------
def _run_read(
    ctx: Any,
    runtime: Any,
    task: RemoteTask,
    snapshot: HostSnapshot,
    result: AgentResult,
) -> AgentResult:
    result.topology_hash_before = snapshot.topology_hash
    result.topology_hash_after = snapshot.topology_hash
    if ctx.cancelled():
        return _fail(runtime, result, TaskRejected("cancelled", "任务被取消"))
    try:
        payload = adapters.build_read_result(snapshot, task)
    except TaskRejected as rejection:
        return _fail(runtime, result, rejection)

    evidence = _open_evidence(runtime, task)
    try:
        if task.params.get("include_raw_output"):
            for name, content in snapshot.raw_outputs:
                evidence.write_text(
                    _evidence_name("raw", name),
                    content,
                    kind="inventory",
                    stage="execute",
                )
        evidence.write_json("inventory.json", payload, kind="inventory", stage="execute")
        manifest = evidence.finalize()
        result.evidence_dir = str(evidence.root)
        result.evidence_manifest = evidence.records()
        result.evidence_status = "complete" if manifest.get("files") else "complete"
    except EvidenceLimitExceeded as exc:
        result.evidence_incomplete = True
        result.warnings.append(f"证据超限: {exc}")
        result.evidence_status = "partial"
    except Exception as exc:
        log.warning("只读证据写入失败: %s", exc)
        result.evidence_incomplete = True
        result.evidence_status = "partial"

    result.result = payload
    result.status = ReportedStatus.FINISHED
    return _finish(runtime, result)


# ---------------------------------------------------------------------------
# destructive
# ---------------------------------------------------------------------------
def _run_destructive(
    ctx: Any,
    runtime: Any,
    task: RemoteTask,
    snapshot: HostSnapshot,
    result: AgentResult,
) -> Optional[AgentResult]:
    dry_run = bool(task.params.get("dry_run")) or bool(runtime.cfg.dry_run)
    try:
        plan = _prepare(runtime, task, snapshot)
        evidence = _open_evidence(runtime, task)
        if not evidence.check_space():
            raise TaskRejected("storage_full", "证据目录剩余空间不足 512MiB")
    except TaskRejected as rejection:
        return _fail(runtime, result, rejection)

    if ctx.cancelled():
        return _fail(runtime, result, TaskRejected("cancelled", "任务被取消"))

    result.topology_hash_before = snapshot.topology_hash
    result.warnings.extend(getattr(plan, "warnings", []))
    result.result = {"plan": _plan_payload(plan)}

    if dry_run:
        try:
            evidence.write_json("plan.json", _plan_payload(plan), kind="plan", stage="confirm")
            evidence.write_json("preflight.json", _snapshot_payload(snapshot), kind="inventory", stage="preflight")
            evidence.finalize()
            result.evidence_dir = str(evidence.root)
            result.evidence_manifest = evidence.records()
        except Exception as exc:
            result.evidence_incomplete = True
            result.warnings.append(f"证据写入失败: {exc}")
        result.warnings.append("dry_run: 未执行任何写命令")
        result.status = ReportedStatus.FINISHED
        return _finish(runtime, result)

    policy_override = (
        not task.require_local_confirm or runtime.agent_cfg.confirm_policy == "always"
    )
    request = _confirm_request(runtime, task, plan, snapshot, policy_override)
    decision = runtime.request_confirm(request)
    result.confirm = safety.confirm_record(
        task, decision, policy_override_ignored=policy_override
    )
    if not decision.approved:
        return _fail(runtime, result, TaskRejected(decision.rejected_code, decision.reason or "操作者拒绝"))

    if ctx.cancelled():
        return _fail(runtime, result, TaskRejected("cancelled", "任务被取消"))

    # 写前复验：拓扑、启动介质、租约、目标身份
    try:
        fresh = runtime.capture_snapshot(fresh=True)
        safety.check_lease(task, runtime.clock.utcnow())
        safety.check_topology(result.topology_hash_before, fresh, snapshot.projection)
        plan = _prepare(runtime, task, fresh)
        snapshot = fresh
        result.topology_hash_before = fresh.topology_hash
    except TaskRejected as rejection:
        return _fail(runtime, result, rejection)

    intent = {
        "task_id": task.task_id,
        "lease_id": task.lease_id,
        "type": task.type.value,
        "state": "executing",
        "started_at": utc_now_iso(),
        "topology_hash": result.topology_hash_before,
        "terminal_state_unknown": False,
    }
    runtime.store.set_current_task(intent)

    started = runtime.client.started(task.task_id, task.lease_id)
    if started.status == 409:
        code = started.error_code
        runtime.store.set_current_task(None)
        if code == "lease_superseded":
            result.warnings.append("lease_superseded before started: 本地丢弃，不上报")
            return None
        if code == "task_terminal":
            code = "task_already_finished"
        return _fail(runtime, result, TaskRejected(code or "task_already_finished", started.error_message))
    if started.status in (404, 410):
        runtime.store.set_current_task(None)
        result.warnings.append(f"任务已不存在 status={started.status}，本地丢弃")
        return None
    if not started.ok:
        runtime.store.set_current_task(None)
        return _fail(
            runtime,
            result,
            TaskRejected(started.error_code or "internal_error", started.error_message),
        )

    runtime.store.mark_executed(task.task_id, task.lease_id)
    result.started_at = rfc3339(runtime.clock.utcnow())

    # 证据：预检/计划/确认
    try:
        evidence.write_json("preflight.json", _snapshot_payload(snapshot), kind="inventory", stage="preflight")
        evidence.write_json("plan.json", _plan_payload(plan), kind="plan", stage="confirm")
        evidence.write_json("confirm.json", result.confirm, kind="json", stage="confirm")
        evidence.write_json("scan-before.json", _snapshot_payload(snapshot), kind="inventory", stage="preflight")
    except Exception as exc:
        result.evidence_incomplete = True
        result.warnings.append(f"证据写入失败: {exc}")

    outcome = _execute_plan(ctx, runtime, task, plan, evidence, result)
    if outcome is not None:
        try:
            evidence.write_json("scan-after.json", _snapshot_payload(outcome), kind="inventory", stage="postcheck")
        except Exception as exc:
            result.evidence_incomplete = True
            result.warnings.append(f"后校验证据失败: {exc}")

    runtime.store.set_current_task(None)
    try:
        evidence.finalize()
        result.evidence_dir = str(evidence.root)
        result.evidence_manifest = evidence.records()
        result.evidence_status = "pending" if result.evidence_manifest else "complete"
    except Exception as exc:
        result.evidence_incomplete = True
        result.evidence_status = "partial"
        result.warnings.append(f"manifest 写入失败: {exc}")
    return _finish(runtime, result)


def _prepare(runtime: Any, task: RemoteTask, snapshot: HostSnapshot) -> Any:
    if task.type is RemoteTaskType.CREATE_RAID:
        return adapters.build_create(snapshot, task.params)
    if task.type is RemoteTaskType.DELETE_RAID:
        return adapters.build_delete(snapshot, task.params)
    if task.type is RemoteTaskType.CLEAR_CONFIG:
        return adapters.build_clear(runtime.cfg, snapshot, task.params)
    raise TaskRejected("unsupported_task_type", "不是破坏性任务", field="type")


def _execute_plan(
    ctx: Any,
    runtime: Any,
    task: RemoteTask,
    plan: Any,
    evidence: TaskEvidence,
    result: AgentResult,
) -> Optional[HostSnapshot]:
    deadline = runtime.clock.utcnow() + timedelta(seconds=int(task.timeout_seconds))
    abort_reason: Dict[str, str] = {}
    stop_watch = threading.Event()

    def should_stop() -> bool:
        if "timeout" in abort_reason:
            return True
        if ctx.cancelled() or runtime.stopping.is_set():
            abort_reason.setdefault("cancel", "cancelled")
            return True
        if task.raw.get("_cancel_requested"):
            abort_reason.setdefault("cancel", "cancelled")
            return True
        if task.raw.get("_lease_superseded"):
            abort_reason.setdefault("lease_superseded", "lease_superseded")
            return True
        if runtime.clock.utcnow() > deadline:
            abort_reason.setdefault("timeout", "timeout")
            return True
        lease = parse_rfc3339(task.lease_expires_at) if task.lease_expires_at else None
        if lease is not None and runtime.clock.utcnow() > lease:
            abort_reason.setdefault("lease", "lease_expired")
            return True
        return False

    watchdog = _CancelWatchdog(runtime, task, ctx, stop_watch)
    watchdog.start()
    cfg = replace(runtime.cfg, dry_run=False)

    def on_raid_progress(done: int, total: int, item: RaidActionResult) -> None:
        _record_raid(runtime, ctx, task, evidence, result, item, done, total)
        if item.status is RaidStatus.TIMEOUT:
            abort_reason.setdefault("timeout", "timeout")

    result.status = ReportedStatus.RUNNING
    _progress(runtime, task, 0, 0, f"开始执行 {task.type.value}")
    try:
        if isinstance(plan, adapters.CreatePlan):
            execute_raid_plans(plan.plans, evidence, cfg, on_progress=on_raid_progress, should_stop=should_stop)
        elif isinstance(plan, adapters.DeletePlan):
            execute_raid_plans(plan.plans, evidence, cfg, on_progress=on_raid_progress, should_stop=should_stop)
        elif isinstance(plan, adapters.ClearPlan):
            execute_raid_plans(plan.plans, evidence, cfg, on_progress=on_raid_progress, should_stop=should_stop)
    finally:
        stop_watch.set()
        watchdog.join(timeout=1.0)

    reason = next(iter(abort_reason.values()), "")
    if reason == "timeout":
        result.fail("timeout", "任务执行超时")
        result.terminal_state_unknown = True
    elif reason == "lease_expired":
        result.fail("lease_expired", "租约在执行中过期")
        result.terminal_state_unknown = True
    elif reason == "lease_superseded":
        result.fail("lease_superseded", "租约被服务端顶替")
        result.terminal_state_unknown = True
    elif reason == "cancelled":
        result.fail("cancelled", "任务被取消")
        result.result["partial"] = _partial_payload(plan, result)
        return None
    else:
        failed = [command for command in result.commands if command.exit_code not in (0, None)]
        timeouts = [
            command
            for command in result.commands
            if command.exit_code == 124
        ]
        if timeouts:
            result.fail("timeout", "厂商命令超时")
            result.terminal_state_unknown = True
        elif failed and len(failed) == len(result.commands):
            result.fail("internal_error", "全部命令失败")
        elif failed:
            result.fail("internal_error", f"{len(failed)} 条命令失败")

    # 后校验：失败保留已完成结果，标 verify_failed
    try:
        after = runtime.capture_snapshot(fresh=True)
        result.topology_hash_after = after.topology_hash
        if result.status is not ReportedStatus.FAILED:
            ok, detail = _verify(plan, after, task)
            result.result["verify"] = detail
            if not ok:
                result.fail("verify_failed", "后校验失败", detail)
                result.status = ReportedStatus.FAILED
        return after
    except Exception as exc:
        log.warning("后校验扫描失败: %s", exc)
        result.warnings.append(f"后校验扫描失败: {exc}")
        if result.status is not ReportedStatus.FAILED:
            result.fail("verify_failed", f"后校验失败: {exc}")
        return None


def _verify(plan: Any, snapshot: HostSnapshot, task: RemoteTask) -> Tuple[bool, Dict[str, Any]]:
    controller_id = task.params["controller_id"]
    if isinstance(plan, adapters.CreatePlan):
        return adapters.verify_create(snapshot, controller_id, plan.layout)
    if isinstance(plan, adapters.DeletePlan):
        return adapters.verify_delete(snapshot, controller_id, plan.volume.volume_id)
    if isinstance(plan, adapters.ClearPlan):
        return adapters.verify_clear(snapshot, controller_id, bool(task.params.get("include_foreign")))
    return True, {}


# ---------------------------------------------------------------------------
# progress / commands / evidence
# ---------------------------------------------------------------------------
class _CancelWatchdog(threading.Thread):
    def __init__(self, runtime: Any, task: RemoteTask, ctx: Any, stop: threading.Event) -> None:
        super().__init__(name="agent-cancel-watch", daemon=True)
        self.runtime = runtime
        self.task = task
        self.ctx = ctx
        self.stop_event = stop

    def run(self) -> None:
        while not self.stop_event.wait(5.0):
            if self.ctx.cancelled():
                return
            try:
                response = self.runtime.client.cancel_requested(self.task.task_id)
            except Exception:
                continue
            if response.ok and isinstance(response.body, dict) and response.body.get("cancel_requested"):
                self.task.raw["_cancel_requested"] = True
                try:
                    self.ctx.task.abort_requested.set()
                except Exception:
                    pass
                return
            if response.status == 409 and response.error_code == "lease_superseded":
                self.task.raw["_lease_superseded"] = True
                return


def _record_raid(
    runtime: Any,
    ctx: Any,
    task: RemoteTask,
    evidence: TaskEvidence,
    result: AgentResult,
    item: RaidActionResult,
    done: int,
    total: int,
) -> None:
    record = CommandRecord(
        seq=len(result.commands) + 1,
        argv=list(item.argv),
        exit_code=item.return_code,
        started_at=rfc3339(runtime.clock.utcnow()),
        duration_ms=int(item.duration_sec * 1000),
        stdout_sha256=hashlib.sha256((item.stdout or "").encode("utf-8")).hexdigest(),
        stderr_tail=(item.stderr or "")[-4000:],
    )
    result.commands.append(record)
    try:
        evidence.write_json(
            f"cmd-{record.seq:03d}.json",
            {"result": item.to_json_obj(), "command": record.to_dict()},
            kind="command_output",
            stage="execute",
        )
    except EvidenceLimitExceeded as exc:
        result.evidence_incomplete = True
        result.warnings.append(str(exc))
    ctx.progress(done, total, f"{item.label} {item.status.value} rc={item.return_code}")
    _progress(runtime, task, done, total, f"{item.label} {item.status.value}")


def _progress(runtime: Any, task: RemoteTask, done: int, total: int, message: str) -> None:
    try:
        response = runtime.client.progress(
            task.task_id,
            task.lease_id,
            {
                "phase": "running",
                "done": done,
                "total": total,
                "message": message,
            },
        )
        if response.ok and isinstance(response.body, dict) and response.body.get("cancel_requested"):
            task.raw["_cancel_requested"] = True
        if response.status == 409 and response.error_code == "lease_superseded":
            task.raw["_lease_superseded"] = True
    except Exception:
        pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _open_evidence(runtime: Any, task: RemoteTask) -> TaskEvidence:
    limits = runtime.limits
    return TaskEvidence(
        runtime.agent_cfg.evidence_dir,
        task.task_id,
        clock=runtime.clock,
        max_files=int(getattr(limits, "max_files", 32)),
        max_file_bytes=int(getattr(limits, "max_file_bytes", 64 * 1024 * 1024)),
        max_total_bytes=int(getattr(limits, "max_total_bytes", 256 * 1024 * 1024)),
    )


def _plan_payload(plan: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"commands": plan.previews()}
    if isinstance(plan, adapters.CreatePlan):
        payload.update(
            {
                "raid_level": plan.layout.level.value,
                "usable_bytes": plan.layout.capacity_bytes,
                "stripe_kb": plan.layout.stripe_kb,
                "init": plan.layout.init,
                "member_serials": [disk.serial for disk in plan.layout.disks],
                "warnings": list(plan.warnings),
            }
        )
    elif isinstance(plan, adapters.DeletePlan):
        payload.update({"vd_id": plan.volume.volume_id, "vd_name": plan.volume.name})
    elif isinstance(plan, adapters.ClearPlan):
        payload.update({"expected_vd_count": plan.vd_count})
    return payload


def _snapshot_payload(snapshot: HostSnapshot) -> Dict[str, Any]:
    return {
        "topology_hash": snapshot.topology_hash,
        "collected_at": snapshot.collected_at,
        "controllers": list(snapshot.controllers),
        "pds": list(snapshot.pds),
        "vds": list(snapshot.vds),
        "boot_media_determined": snapshot.boot_media.determined,
        "storage": snapshot.storage,
    }


def _partial_payload(plan: Any, result: AgentResult) -> Dict[str, Any]:
    completed = [command.argv for command in result.commands if command.exit_code == 0]
    total = len(plan.plans)
    return {"completed": completed, "not_started": max(0, total - len(result.commands))}


def _confirm_request(
    runtime: Any,
    task: RemoteTask,
    plan: Any,
    snapshot: HostSnapshot,
    policy_override: bool,
) -> ConfirmRequest:
    targets: List[str] = []
    if isinstance(plan, adapters.CreatePlan):
        for disk in plan.layout.disks:
            targets.append(f"{disk.serial} / {disk.wwn} / {disk.size_bytes} bytes ({disk.disk_id})")
        stripe_label = f"{plan.layout.stripe_kb}K" if plan.layout.stripe_kb else "控制器默认"
        init_label = "快速初始化 (fast)" if plan.layout.init == "fast" else "不显式指定 (none)"
        targets.append(
            f"新建 {plan.layout.level.value} 名称={plan.layout.name} "
            f"可用容量={plan.layout.capacity_bytes} 条带={stripe_label} 初始化={init_label}"
        )
    elif isinstance(plan, adapters.DeletePlan):
        targets.append(
            f"VD {plan.volume.volume_id} 名称={plan.volume.name} 容量={plan.volume.size_bytes}"
        )
    elif isinstance(plan, adapters.ClearPlan):
        targets.append(f"控制器 {task.params['controller_id']} 全部 VD（预计 {plan.vd_count} 个）")

    return ConfirmRequest(
        task_id=task.task_id,
        task_type=task.type.value,
        server_sn=task.server_sn,
        created_by=task.created_by,
        summary=[
            f"类型 {task.type.value}",
            f"发起人 {task.created_by or '-'}",
            f"服务器 {task.server_sn}",
            f"超时 {task.timeout_seconds}s",
        ],
        targets=targets,
        commands=plan.previews(),
        topology_hash=snapshot.topology_hash,
        timeout_seconds=runtime.agent_cfg.confirm_timeout_seconds,
        default_operator=runtime.default_operator(),
        policy_override_ignored=policy_override,
        evidence_dir=str(runtime.agent_cfg.evidence_dir / task.task_id),
    )


def _evidence_name(prefix: str, raw_name: str) -> str:
    base = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(raw_name or ""))
    suffix = ""
    if "." in base:
        suffix = "." + base.rsplit(".", 1)[1].lower()
    if suffix not in {".json", ".txt", ".log", ".csv", ".png", ".jpg", ".gz"}:
        suffix = ".txt"
    stem = base[: 120 - len(prefix) - len(suffix) - 1]
    return f"{prefix}_{stem}{suffix}"


def _finish(runtime: Any, result: AgentResult) -> AgentResult:
    if result.status is ReportedStatus.RUNNING and not result.error_code:
        result.status = ReportedStatus.FINISHED
    now = runtime.clock.utcnow()
    result.finished_at = rfc3339(now)
    try:
        started = parse_rfc3339(result.started_at)
        result.duration_ms = int((now - started).total_seconds() * 1000) if started else 0
    except Exception:
        result.duration_ms = 0
    return result


def _fail(runtime: Any, result: AgentResult, rejection: TaskRejected) -> AgentResult:
    detail = dict(rejection.detail)
    if rejection.field:
        detail.setdefault("field", rejection.field)
    result.fail(rejection.error_code, rejection.message, detail)
    return _finish(runtime, result)


__all__ = ["build_remote_job", "execute"]
