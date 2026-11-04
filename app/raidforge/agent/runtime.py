"""Agent 运行时：组装线程、状态、确认、排水与 UI 通知。"""

from __future__ import annotations

import getpass
import logging
import logging.handlers
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import adapters
from .clock import Clock, SystemClock, parse_rfc3339, rfc3339
from .client import AgentClient
from .config import AgentConfig, normalize_server_url
from .evidence import TaskEvidence, gc_evidence
from .evidence_uploader import EvidenceUploader
from .executor import build_remote_job
from .heartbeat import HeartbeatLoop
from .models import (
    AgentLimits,
    AgentResult,
    AgentStatusSnapshot,
    ConfirmDecision,
    ConfirmRequest,
    ConnectionState,
    RemoteTask,
    RemoteTaskType,
    ReportedStatus,
    TaskRejected,
)
from .poller import TaskPoller
from .queue import RemoteAdmission
from .reporter import ResultReporter
from .snapshot import SnapshotProvider
from .state import StateStore
from .version import AGENT_VERSION

log = logging.getLogger(__name__)

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


class _TokenFilter(logging.Filter):
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self.token:
            try:
                if isinstance(record.msg, str) and self.token in record.msg:
                    record.msg = record.msg.replace(self.token, "***")
                if record.args:
                    record.args = tuple(
                        str(item).replace(self.token, "***") if isinstance(item, str) else item
                        for item in record.args
                    )
            except Exception:
                pass
        return True


class AgentRuntime:
    def __init__(
        self,
        state: Any,
        ui_post: Optional[Callable[[Any], None]] = None,
        confirm_sink: Optional[Callable[[ConfirmRequest], ConfirmDecision]] = None,
        agent_cfg: Optional[AgentConfig] = None,
        clock: Optional[Clock] = None,
        runner: Any = None,
    ) -> None:
        self.state = state
        self.cfg = state.cfg
        self.clock = clock or SystemClock()
        self.agent_cfg = agent_cfg or AgentConfig.load()
        self.server_sn = self._resolve_server_sn()
        self.agent_cfg.agent_id = self._derive_agent_id()
        self.ui_post = ui_post
        self.confirm_sink = confirm_sink

        if self.agent_cfg.configured:
            self._setup_logging()
        self.store = StateStore(self.agent_cfg.state_dir, clock=self.clock)
        self.snapshot = SnapshotProvider(
            self.cfg,
            self.server_sn,
            Path(self.agent_cfg.evidence_dir),
            clock=self.clock,
            runner=runner,
        )
        self.client = AgentClient(self.agent_cfg, clock=self.clock)
        self.reporter = ResultReporter(self.client, self.store, self.clock)
        self.uploader = EvidenceUploader(
            self.client,
            self.store,
            Path(self.agent_cfg.evidence_dir),
            reporter=self.reporter,
            clock=self.clock,
        )
        self.admission = RemoteAdmission(
            getattr(state, "task_runner", None),
            self._make_item,
            clock=self.clock,
            queue_callback=self.emit_status,
        )
        self.limits = AgentLimits()
        self.connection = (
            ConnectionState.CONNECTING if self.agent_cfg.configured else ConnectionState.UNCONFIGURED
        )
        self.enabled = True
        self.protocol_ok = True
        self.paused = False
        self.pause_reason = ""
        self.note = "" if self.agent_cfg.configured else "未配置"
        self.last_error: Dict[str, Any] = {"code": "", "at": "", "message": ""}
        self.server_delta = 0
        self.stopping = threading.Event()

        self._started = False
        self._supervisor: Optional[threading.Thread] = None
        self._heartbeat: Optional[HeartbeatLoop] = None
        self._poller: Optional[TaskPoller] = None
        self._retransmit: Optional[threading.Thread] = None
        self._recovery_pending = False
        self._confirm_pending: Optional[ConfirmRequest] = None
        self._confirm_lock = threading.Lock()
        self._active_lock = threading.RLock()
        self._active_task: Optional[RemoteTask] = None
        self._active_started: str = ""
        self._items: Dict[str, Any] = {}
        self._known: Dict[str, RemoteTask] = {}
        self._last_gc = 0.0
        self._pending_webplane: Optional[tuple[str, str]] = None

    # -- identity -----------------------------------------------------------
    def _resolve_server_sn(self) -> str:
        detected = str(getattr(self.state, "server_sn", "") or "").strip()
        override = str(self.agent_cfg.server_sn_override or "").strip()
        unusable = (
            not detected
            or detected.upper().startswith("UNK")
            or detected.upper() in {"UNKNOWN", "NONE", "TO BE FILLED BY O.E.M."}
        )
        if override and unusable:
            log.warning("server_sn 探测失败，使用 server_sn_override=%s", override)
            detected = override
        elif override and detected and override != detected:
            log.warning("server_sn_override=%s 与探测值 %s 不一致；以探测值为准", override, detected)
        detected = _SANITIZE_RE.sub("", detected)[:128]
        return detected or "UNKNOWN"

    def _derive_agent_id(self) -> str:
        return ("rf-" + _SANITIZE_RE.sub("", self.server_sn))[:64]

    def _setup_logging(self) -> None:
        logger = logging.getLogger("raidforge.agent")
        logger.setLevel(logging.INFO)
        for handler in list(logger.handlers):
            if getattr(handler, "_raidforge_agent", False) or getattr(handler, "_raidforge_ui", False):
                logger.removeHandler(handler)
        try:
            from ..ui import logbus
        except Exception:  # pragma: no cover - UI unavailable in headless modes
            logbus = None  # type: ignore[assignment]
        if logbus is not None:
            logbus.add_secret(self.agent_cfg.token)
            ui_handler = logbus.active()
            if ui_handler is not None:
                ui_handler._raidforge_ui = True  # type: ignore[attr-defined]
                logger.propagate = False
                logger.addHandler(ui_handler)
            else:
                logger.propagate = True
        try:
            log_dir = Path(self.agent_cfg.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / "agent.log"
        except OSError:
            path = Path(self.agent_cfg.state_dir) / "agent.log"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
        try:
            handler = logging.handlers.RotatingFileHandler(
                str(path), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            handler._raidforge_agent = True  # type: ignore[attr-defined]
            handler.setFormatter(
                logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
            handler.addFilter(_TokenFilter(self.agent_cfg.token))
            logger.addHandler(handler)
        except OSError as exc:
            log.warning("无法打开 agent 日志 %s: %s", path, exc)

    # -- lifecycle ----------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self.agent_cfg.configured

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if not self.configured:
            self.connection = ConnectionState.UNCONFIGURED
            self.note = "未配置"
            self.emit_status()
            return
        if not self.store.acquire_process_lock():
            self.connection = ConnectionState.DISABLED
            self.note = "另一个 Agent 实例正在运行"
            self.emit_status()
            return
        self._supervisor = threading.Thread(target=self._supervise, name="agent-supervisor", daemon=True)
        self._supervisor.start()
        self.emit_status()

    def _supervise(self) -> None:
        try:
            state = self.store.load()
            self.store.set_identity(self.server_sn, self.agent_cfg.agent_id)
            paused, reason = self.store.is_paused()
            self.paused = paused
            self.pause_reason = reason
            self._recover(state)

            self._retransmit = threading.Thread(
                target=self._retransmit_loop, name="agent-retransmit", daemon=True
            )
            self._retransmit.start()
            self.reporter.drain_once()
            self.uploader.drain_once()

            self._heartbeat = HeartbeatLoop(
                self.client, self.snapshot, self.store, self, self.agent_cfg, self.clock
            )
            self._poller = TaskPoller(
                self.client, self.admission, self.store, self, self.clock
            )
            self._heartbeat.start()
            self._poller.start()
            self.emit_status()
        except Exception as exc:  # pragma: no cover - 启动失败不得影响 TUI
            log.exception("Agent 启动失败")
            self.connection = ConnectionState.DISABLED
            self.note = f"启动失败: {exc}"
            self.emit_status()

    def stop(self, grace: float = 10.0) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        if not self.configured:
            return
        if self._poller is not None:
            self._poller.stop()
        if self._heartbeat is not None:
            self._heartbeat.stop()

        for task in self.admission.clear_unstarted():
            self._release(task, "agent_stopping")
            self._known.pop(task.task_id, None)
            self._items.pop(task.task_id, None)

        deadline = time.monotonic() + max(0.0, grace)
        while self.active_task() is not None and time.monotonic() < deadline:
            time.sleep(0.1)

        try:
            if self._retransmit is not None:
                self._retransmit.join(timeout=1.0)
            self.reporter.drain_once(limit=5)
            self.uploader.drain_once(limit=5)
        except Exception:
            pass
        self.store.release_process_lock()
        self.emit_status()

    # -- recovery -----------------------------------------------------------
    def _recover(self, state: Any) -> None:
        current = state.current_task
        if not isinstance(current, dict) or current.get("state") != "executing":
            return
        task_id = str(current.get("task_id") or "")
        lease_id = str(current.get("lease_id") or "")
        task_type = str(current.get("type") or "")
        destructive = task_type in {
            RemoteTaskType.CREATE_RAID.value,
            RemoteTaskType.DELETE_RAID.value,
            RemoteTaskType.CLEAR_CONFIG.value,
            RemoteTaskType.ERASE.value,
        }
        if not task_id or not lease_id:
            self.store.set_current_task(None)
            return
        response = self.client.get_task(task_id)
        if response.status in (404, 410):
            self.store.set_current_task(None)
            log.info("重启恢复：任务 %s 已不存在，丢弃", task_id)
            return
        if response.ok and isinstance(response.body, dict):
            server_task = response.body.get("task")
            if not isinstance(server_task, dict):
                server_task = response.body
            status = str(server_task.get("status") or "")
            server_lease = str(server_task.get("lease_id") or "")
            if status in {"finished", "failed"} or (server_lease and server_lease != lease_id):
                self.store.set_current_task(None)
                log.info("重启恢复：任务 %s 已终态或被顶替，丢弃", task_id)
                return
        result = AgentResult(
            task_id=task_id,
            agent_id=self.agent_cfg.agent_id,
            lease_id=lease_id,
            started_at=str(current.get("started_at") or rfc3339(self.clock.utcnow())),
            terminal_state_unknown=destructive,
        )
        result.fail(
            "agent_restarted",
            "Agent 在执行中被重启，无法确认最终状态",
            {"task_type": task_type},
        )
        now = self.clock.utcnow()
        result.finished_at = rfc3339(now)
        started = parse_rfc3339(result.started_at)
        result.duration_ms = int((now - started).total_seconds() * 1000) if started else 0
        self.reporter.persist(result.to_dict())
        self.store.set_current_task(None)
        if destructive:
            self.pause("agent_restarted")

    # -- loops --------------------------------------------------------------
    def _retransmit_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                self.reporter.drain_once()
                self.uploader.drain_once()
                if self._recovery_pending:
                    self._recovery_pending = False
                now = self.clock.monotonic()
                if now - self._last_gc > 300:
                    self._last_gc = now
                    gc_evidence(Path(self.agent_cfg.evidence_dir))
            except Exception as exc:  # pragma: no cover
                log.exception("补传循环异常: %s", exc)
            if self.stopping.wait(1.0):
                break

    # -- heartbeat callbacks ------------------------------------------------
    def heartbeat_interval(self) -> int:
        if self.connection in (ConnectionState.DISABLED, ConnectionState.AUTH_FAILED):
            return 60
        if self.connection is ConnectionState.MAINTENANCE:
            return 30
        return int(self.agent_cfg.heartbeat_interval)

    def heartbeat_status(self) -> str:
        if self.connection is ConnectionState.DISABLED:
            return "disabled"
        if self._confirm_pending is not None:
            return "confirming"
        if self.active_task() is not None:
            return "busy"
        return "idle"

    def current_task_payload(self) -> Optional[Dict[str, Any]]:
        task = self.active_task()
        if task is None:
            return None
        started = self._active_started or rfc3339(self.clock.utcnow())
        deadline = ""
        started_at = parse_rfc3339(started)
        if started_at is not None:
            from datetime import timedelta

            deadline = rfc3339(started_at + timedelta(seconds=int(task.timeout_seconds)))
        return {
            "task_id": task.task_id,
            "type": task.type.value,
            "started_at": started,
            "deadline_at": deadline,
        }

    def queue_depth(self) -> int:
        return self.admission.remote_queued()

    def server_time_delta(self) -> int:
        return int(self.server_delta)

    def on_heartbeat_ok(self, body: Dict[str, Any]) -> None:
        if body.get("enabled") is False:
            self.on_disabled("agent_disabled")
            return
        protocol = body.get("protocol") or {}
        try:
            minimum = int(protocol.get("min") or 1)
        except (TypeError, ValueError):
            minimum = 1
        if minimum > 1:
            self.on_protocol_too_old()
            return
        poll = body.get("poll") or {}
        if poll.get("mode") and poll.get("mode") != "short":
            log.warning("服务端下发 poll.mode=%s；v1 回退 short", poll.get("mode"))
        limits = body.get("limits") or {}
        if limits:
            evidence = limits.get("evidence") or {}
            self.limits = AgentLimits(
                timeout_seconds_max=int(limits.get("timeout_seconds_max") or 7200),
                max_files=int(evidence.get("max_files") or 32),
                max_file_bytes=int(evidence.get("max_file_bytes") or 64 * 1024 * 1024),
                max_total_bytes=int(evidence.get("max_total_bytes") or 256 * 1024 * 1024),
            )
        server_time = parse_rfc3339(str(body.get("server_time") or ""))
        if server_time is not None:
            self.server_delta = int((server_time - self.clock.utcnow()).total_seconds())

        previous = self.connection
        self.connection = ConnectionState.CONNECTED
        self.enabled = True
        self.protocol_ok = True
        self.note = ""
        if previous is not ConnectionState.CONNECTED:
            self._recovery_pending = True
            log.info("与控制面恢复连接")
        self.emit_status()

    def on_heartbeat_failure(self, count: int) -> None:
        if count >= 6 and self.connection is not ConnectionState.DISABLED:
            self.connection = ConnectionState.OFFLINE
            self.note = "服务端离线"
            self.emit_status()

    def on_auth_failed(self, note: str) -> None:
        self.connection = ConnectionState.AUTH_FAILED
        self.note = note
        self.set_last_error("auth_failed", note)
        self.emit_status()

    def on_maintenance(self) -> None:
        self.connection = ConnectionState.MAINTENANCE
        self.note = "服务端维护中"
        self.emit_status()

    def on_protocol_too_old(self) -> None:
        self.connection = ConnectionState.PROTOCOL_TOO_OLD
        self.protocol_ok = False
        self.note = "协议版本过旧，请升级"
        self.set_last_error("protocol_too_old", self.note)
        self.emit_status()

    def on_disabled(self, code: str) -> None:
        self.connection = ConnectionState.DISABLED
        self.enabled = False
        self.note = "已被服务端禁用" if code == "agent_disabled" else "代理类型不被接受"
        self.set_last_error(code or "agent_disabled", self.note)
        for task in self.admission.clear_unstarted():
            self._deliver_failure(task, code or "agent_disabled", self.note)
            self._release(task, code or "agent_disabled")
        active = self.active_task()
        if active is not None:
            self.abort_task(active.task_id)
        self.emit_status()

    # -- polling ------------------------------------------------------------
    def can_poll(self) -> bool:
        return (
            self.connection is ConnectionState.CONNECTED
            and self.enabled
            and self.protocol_ok
            and not self.paused
            and not self.stopping.is_set()
            and not self._recovery_pending
            and self.admission.can_poll(self.paused)
        )

    def rejection_result(self, envelope: Dict[str, Any], rejection: TaskRejected) -> AgentResult:
        task_id = str(envelope.get("task_id") or "")
        lease_id = str(envelope.get("lease_id") or "")
        result = AgentResult(
            task_id=task_id,
            agent_id=self.agent_cfg.agent_id,
            lease_id=lease_id,
            started_at=rfc3339(self.clock.utcnow()),
            finished_at=rfc3339(self.clock.utcnow()),
        )
        detail = dict(rejection.detail)
        if rejection.field:
            detail.setdefault("field", rejection.field)
        result.fail(rejection.error_code, rejection.message, detail)
        return result

    def report_rejection(self, envelope: Dict[str, Any], rejection: TaskRejected) -> None:
        task_id = str(envelope.get("task_id") or "")
        lease_id = str(envelope.get("lease_id") or "")
        if not task_id or not lease_id:
            log.warning("拒绝任务但缺少 task_id/lease_id: %s", rejection.error_code)
            return
        result = self.rejection_result(envelope, rejection)
        self.reporter.deliver(result.to_dict())
        self.store.remember_task(
            {"task_id": task_id, "lease_id": lease_id, "status": "failed", "at": result.finished_at}
        )

    def release_task(self, task: RemoteTask, reason: str = "") -> None:
        self._release(task, reason)

    def _release(self, task: RemoteTask, reason: str) -> None:
        try:
            self.client.release(task.task_id, task.lease_id, reason)
        except Exception:
            pass

    # -- executor callbacks -------------------------------------------------
    def capture_snapshot(self, fresh: bool = False):
        return self.snapshot.capture(fresh=fresh)

    def on_task_running(self, task: RemoteTask) -> None:
        with self._active_lock:
            self._active_task = task
            self._active_started = rfc3339(self.clock.utcnow())
        self.emit_status()

    def on_task_finished(self, task: RemoteTask) -> None:
        with self._active_lock:
            if self._active_task is not None and self._active_task.task_id == task.task_id:
                self._active_task = None
                self._active_started = ""
        pending = self._pending_webplane
        self._pending_webplane = None
        self._known.pop(task.task_id, None)
        self._items.pop(task.task_id, None)
        self.emit_status()
        if pending is not None:
            try:
                self.apply_webplane(*pending)
            except ValueError as exc:
                self.set_last_error("invalid_params", str(exc))

    def active_task(self) -> Optional[RemoteTask]:
        with self._active_lock:
            return self._active_task

    def finish_remote(self, task: RemoteTask, result: AgentResult) -> None:
        self.store.set_current_task(None)
        self.admission.mark_finished(task.task_id)
        self.store.remember_task(
            {
                "task_id": task.task_id,
                "lease_id": task.lease_id,
                "status": result.status.value,
                "error_code": result.error_code,
                "at": result.finished_at,
            }
        )
        payload = result.to_dict()
        self.reporter.deliver(payload)
        if result.evidence_manifest:
            try:
                self.uploader.enqueue(
                    task.task_id,
                    task.lease_id,
                    [record.to_dict() for record in result.evidence_manifest],
                    finished_at=result.finished_at,
                )
            except Exception as exc:
                log.warning("证据入队失败: %s", exc)
        if task.type is RemoteTaskType.REFRESH_HARDWARE and result.status is ReportedStatus.FINISHED:
            self.resume("refresh_hardware 成功后恢复")
        if task.destructive and (
            result.terminal_state_unknown
            or result.error_code in {"timeout", "internal_error", "lease_superseded", "agent_restarted"}
        ):
            self.pause(result.error_code or "terminal_state_unknown")
        self.on_task_finished(task)
        self._emit_finished(task, result)

    def internal_error_result(self, task: RemoteTask, exc: Exception) -> AgentResult:
        result = AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_cfg.agent_id,
            lease_id=task.lease_id,
            started_at=self.current_task_payload().get("started_at", "") if self.current_task_payload() else "",
            finished_at=rfc3339(self.clock.utcnow()),
            terminal_state_unknown=task.destructive,
        )
        result.fail("internal_error", f"{type(exc).__name__}: {exc}")
        return result

    def pause_after_failure(self, task: RemoteTask, message: str) -> None:
        if task.destructive:
            self.pause("terminal_state_unknown")
            self.set_last_error("internal_error", message)

    # -- abort --------------------------------------------------------------
    def abort_task(self, task_id: str) -> bool:
        item = self._items.get(task_id)
        if item is None:
            return False
        try:
            item.abort_requested.set()
        except Exception:
            pass
        task = self._known.get(task_id)
        if task is not None:
            task.raw["_cancel_requested"] = True
        runner = getattr(self.state, "task_runner", None)
        if runner is not None:
            try:
                runner.abort_task(item.id)
            except Exception:
                pass
        return True

    def handle_abort_current(self, task: RemoteTask) -> AgentResult:
        target = str(task.params.get("target_task_id") or "")
        active = self.active_task()
        effective = active is not None and (not target or target == active.task_id)
        if effective and active is not None:
            self.abort_task(active.task_id)
        result = AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_cfg.agent_id,
            lease_id=task.lease_id,
            started_at=rfc3339(self.clock.utcnow()),
            finished_at=rfc3339(self.clock.utcnow()),
        )
        result.status = ReportedStatus.FINISHED
        result.result = {
            "aborted_task_id": active.task_id if effective and active is not None else None,
            "abort_effective": bool(effective),
            "reason": task.params.get("reason") or ("abort_requested" if effective else "no_active_task"),
        }
        self.reporter.deliver(result.to_dict())
        self.store.remember_task(
            {
                "task_id": task.task_id,
                "lease_id": task.lease_id,
                "status": "finished",
                "at": result.finished_at,
            }
        )
        return result

    # -- queue --------------------------------------------------------------
    def _make_item(self, task: RemoteTask) -> Any:
        from ..ui.models import TaskKind
        from ..ui.tasks import make_task

        item = make_task(
            TaskKind.REMOTE,
            f"远程 {task.type.value} {task.task_id[-6:]}",
        )
        item.remote_task_id = task.task_id
        item.origin = "remote"
        item.priority = 100
        self._items[task.task_id] = item
        self._known[task.task_id] = task
        return item

    def remote_job(self, task: RemoteTask) -> Any:
        return build_remote_job(self, task)

    def supersede(self, task_id: str) -> None:
        task = self._known.get(task_id)
        if task is None:
            return
        result = AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_cfg.agent_id,
            lease_id=task.lease_id,
            started_at=rfc3339(self.clock.utcnow()),
            finished_at=rfc3339(self.clock.utcnow()),
        )
        result.status = ReportedStatus.FINISHED
        result.warnings.append("superseded")
        result.result = {"superseded": True}
        self.reporter.deliver(result.to_dict())
        self._known.pop(task_id, None)
        self._items.pop(task_id, None)

    def _deliver_failure(self, task: RemoteTask, code: str, message: str) -> None:
        result = AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_cfg.agent_id,
            lease_id=task.lease_id,
            started_at=rfc3339(self.clock.utcnow()),
            finished_at=rfc3339(self.clock.utcnow()),
        )
        result.fail(code, message)
        self.reporter.deliver(result.to_dict())

    # -- confirm ------------------------------------------------------------
    def request_confirm(self, request: ConfirmRequest) -> ConfirmDecision:
        if self.confirm_sink is not None:
            decision = self.confirm_sink(request)
            if decision is None:
                decision = ConfirmDecision(False, operator=self.default_operator(), reason="confirm_rejected")
            if not decision.operator:
                decision.operator = self.default_operator()
            return decision
        if self.ui_post is None:
            return ConfirmDecision(False, operator=self.default_operator(), reason="confirm_timeout")
        with self._confirm_lock:
            self._confirm_pending = request
        self._post_confirm(request)
        if not request.done.wait(float(request.timeout_seconds)):
            decision = ConfirmDecision(
                approved=False,
                operator=self.default_operator(),
                reason="confirm_timeout",
            )
            request.decision = decision
        else:
            decision = request.decision or ConfirmDecision(
                approved=False, operator=self.default_operator(), reason="confirm_rejected"
            )
        if not decision.operator:
            decision.operator = self.default_operator()
        with self._confirm_lock:
            self._confirm_pending = None
        self.emit_status()
        return decision

    def _post_confirm(self, request: ConfirmRequest) -> None:
        try:
            from ..ui.messages import RemoteTaskConfirmRequested

            self._post(RemoteTaskConfirmRequested(request))
        except Exception:
            request.done.set()

    def default_operator(self) -> str:
        return self.resolve_operator("")

    def resolve_operator(self, value: str) -> str:
        candidate = (value or "").strip()
        if not candidate:
            candidate = os.environ.get("RF_OPERATOR", "").strip()
        if not candidate:
            candidate = str(self.agent_cfg.operator_name or "").strip()
        if not candidate:
            try:
                candidate = getpass.getuser()
            except Exception:
                candidate = "root"
        return _SANITIZE_RE.sub("", candidate)[:64] or "root"

    # -- control plane ------------------------------------------------------
    def apply_webplane(self, server_url: str, api_prefix: Optional[str] = None) -> str:
        """Point the next heartbeat/poll at a new control plane immediately."""

        normalized = normalize_server_url(server_url)
        if not normalized:
            raise ValueError(f"invalid webplane URL: {server_url!r}")
        prefix = str(api_prefix or self.agent_cfg.api_prefix or "/api/raidforge").strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        prefix = "/" + prefix.strip("/")
        changed = (
            normalized != self.agent_cfg.server_url
            or prefix != self.agent_cfg.api_prefix
        )
        self.agent_cfg.server_url = normalized
        self.agent_cfg.api_prefix = prefix
        self._pending_webplane = None
        if not changed:
            return normalized
        self.connection = ConnectionState.CONNECTING
        self.note = "控制面地址已更新"
        self.last_error = {"code": "", "at": "", "message": ""}
        self.emit_status()
        try:
            self.state.evidence.emit(
                "agent_webplane_changed", server_url=normalized, api_prefix=prefix
            )
        except Exception:
            pass
        if self._heartbeat is not None:
            self._heartbeat.wake()
        if self._poller is not None:
            self._poller.wake()
        if not self.stopping.is_set() and self._supervisor is None:
            self._started = False
            self.start()
        return normalized

    def request_webplane(self, server_url: str, api_prefix: Optional[str] = None) -> bool:
        """Apply now, or defer until the active remote task finishes."""

        normalized = normalize_server_url(server_url)
        if not normalized:
            raise ValueError(f"invalid webplane URL: {server_url!r}")
        if self.active_task() is not None:
            self._pending_webplane = (
                normalized,
                str(api_prefix or self.agent_cfg.api_prefix or "/api/raidforge"),
            )
            self.note = "控制面变更等待任务结束"
            self.emit_status()
            return False
        self.apply_webplane(normalized, api_prefix)
        return True

    # -- pause / status -----------------------------------------------------
    def pause(self, reason: str) -> None:
        if self.paused:
            return
        self.paused = True
        self.pause_reason = reason
        self.store.set_paused(True, reason)
        self.note = f"已暂停: {reason}"
        log.warning("Agent 队列暂停: %s", reason)
        self.emit_status()

    def resume(self, reason: str = "") -> None:
        if not self.paused:
            return
        self.paused = False
        self.pause_reason = ""
        self.store.set_paused(False)
        self.note = reason or ""
        log.info("Agent 队列恢复")
        self.emit_status()

    def notify_refresh_success(self, scope: str = "") -> None:
        if self.paused:
            self.resume("refresh_hardware 成功后恢复")

    def set_last_error(self, code: str, message: str) -> None:
        self.last_error = {
            "code": code,
            "at": rfc3339(self.clock.utcnow()),
            "message": str(message)[:300],
        }

    def status_snapshot(self) -> AgentStatusSnapshot:
        task = self.active_task()
        minutes = 0
        if task is not None and self._active_started:
            started = parse_rfc3339(self._active_started)
            if started is not None:
                minutes = max(0, int((self.clock.utcnow() - started).total_seconds() // 60))
        return AgentStatusSnapshot(
            connection=self.connection,
            text=self.note,
            queue_depth=self.queue_depth(),
            queue_max=self.agent_cfg.queue_max,
            paused=self.paused,
            current_task=task.task_id if task else "",
            current_type=task.type.value if task else "",
            current_minutes=minutes,
            note=self.note,
            last_error=str(self.last_error.get("code") or ""),
        )

    def emit_status(self) -> None:
        if self.ui_post is None:
            return
        try:
            from ..ui.messages import AgentStatusChanged, QueueLengthChanged

            snapshot = self.status_snapshot()
            self._post(AgentStatusChanged(snapshot))
            self._post(QueueLengthChanged(snapshot.queue_depth, snapshot.queue_max))
        except Exception:
            pass

    def _emit_finished(self, task: RemoteTask, result: AgentResult) -> None:
        if self.ui_post is None:
            return
        try:
            from ..ui.messages import TaskFinished

            self._post(TaskFinished(task.task_id, result.status.value, result.error_code or ""))
        except Exception:
            pass

    def _post(self, message: Any) -> None:
        if self.ui_post is not None:
            self.ui_post(message)


def build_agent(
    state: Any,
    ui_post: Optional[Callable[[Any], None]] = None,
    confirm_sink: Optional[Callable[[ConfirmRequest], ConfirmDecision]] = None,
) -> Optional[AgentRuntime]:
    # 单元测试默认不自动联网；需要真实 Agent 的测试显式设置 RF_AGENT_FORCE_START=1。
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("RF_AGENT_FORCE_START") != "1":
        return None
    try:
        return AgentRuntime(state, ui_post=ui_post, confirm_sink=confirm_sink)
    except Exception as exc:  # pragma: no cover - Agent 故障不得阻止 TUI
        logging.getLogger(__name__).exception("Agent 初始化失败: %s", exc)
        return None


__all__ = ["AgentRuntime", "build_agent"]
