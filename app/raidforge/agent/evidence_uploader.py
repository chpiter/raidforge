"""证据逐文件上传与退避重试。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .client import AgentClient, Response
from .clock import Clock, SystemClock, parse_rfc3339, rfc3339
from .evidence import TaskEvidence
from .reporter import ResultReporter
from .state import StateStore

log = logging.getLogger(__name__)

EVIDENCE_DELAYS = (30.0, 60.0, 300.0)
MAX_ATTEMPTS = 10
MAX_AGE_SECONDS = 24 * 3600


class EvidenceUploader:
    def __init__(
        self,
        client: AgentClient,
        store: StateStore,
        evidence_root,
        reporter: Optional[ResultReporter] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.client = client
        self.store = store
        self.evidence_root = evidence_root
        self.reporter = reporter
        self.clock = clock or SystemClock()

    # -- public -------------------------------------------------------------
    def enqueue(
        self,
        task_id: str,
        lease_id: str,
        manifest: List[Dict[str, Any]],
        finished_at: str = "",
    ) -> None:
        """只登记待上传文件；由 retransmit 线程按退避上传。"""

        if not manifest:
            self._update(task_id, "complete", [], False)
            return
        self.store.put_pending_evidence(
            {
                "task_id": task_id,
                "lease_id": lease_id,
                "finished_at": finished_at or rfc3339(self.clock.utcnow()),
                "attempts": 0,
                "last_attempt_epoch": 0.0,
                "next_at_epoch": 0.0,
                "manifest": list(manifest),
            }
        )

    def upload_task(
        self,
        task_id: str,
        lease_id: str,
        evidence: TaskEvidence,
        finished_at: str = "",
        manifest: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        files = list(manifest) if manifest is not None else list(evidence.manifest().get("files") or [])
        if not files:
            self._update(task_id, "complete", [], False)
            return "complete"

        payload = {
            "task_id": task_id,
            "lease_id": lease_id,
            "finished_at": finished_at or rfc3339(self.clock.utcnow()),
            "attempts": 0,
            "last_attempt_epoch": 0.0,
            "next_at_epoch": 0.0,
            "manifest": files,
        }
        self.store.put_pending_evidence(payload)
        ok, terminal = self._attempt(task_id, lease_id, evidence, files)
        if ok:
            self.store.drop_pending_evidence(task_id)
            self._update(task_id, "complete", files, False)
            return "complete"
        if terminal:
            self.store.drop_pending_evidence(task_id)
            self._update(task_id, "partial", files, True)
            return "partial"

        payload["attempts"] = 1
        payload["last_attempt_epoch"] = time.time()
        payload["next_at_epoch"] = time.time() + EVIDENCE_DELAYS[0]
        self.store.put_pending_evidence(payload)
        return "pending"

    def drain_once(self, limit: int = 5) -> int:
        completed = 0
        now_epoch = time.time()
        now_utc = self.clock.utcnow()
        for _path, payload in self.store.iter_pending_evidence():
            if completed >= limit:
                break
            task_id = str(payload.get("task_id") or "")
            lease_id = str(payload.get("lease_id") or "")
            if float(payload.get("next_at_epoch") or 0) > now_epoch:
                continue
            finished = parse_rfc3339(str(payload.get("finished_at") or ""))
            if finished is not None and (now_utc - finished).total_seconds() > MAX_AGE_SECONDS:
                self.store.drop_pending_evidence(task_id)
                self._update(task_id, "partial", [], True)
                continue
            attempts = int(payload.get("attempts") or 0) + 1
            evidence = TaskEvidence(self.evidence_root, task_id, clock=self.clock)
            files = list(payload.get("manifest") or [])
            if not files:
                try:
                    files = list(evidence.manifest().get("files") or [])
                except Exception:
                    files = []
            if not files:
                self.store.drop_pending_evidence(task_id)
                self._update(task_id, "partial", [], True)
                continue
            ok, terminal = self._attempt(task_id, lease_id, evidence, files)
            if ok:
                self.store.drop_pending_evidence(task_id)
                self._update(task_id, "complete", files, False)
                completed += 1
                continue
            if terminal or attempts >= MAX_ATTEMPTS:
                self.store.drop_pending_evidence(task_id)
                self._update(task_id, "partial", files, True)
                completed += 1
                continue
            payload["attempts"] = attempts
            payload["last_attempt_epoch"] = time.time()
            payload["manifest"] = files
            delay = EVIDENCE_DELAYS[min(attempts - 1, len(EVIDENCE_DELAYS) - 1)]
            payload["next_at_epoch"] = time.time() + delay
            self.store.put_pending_evidence(payload)
        return completed

    # -- internals ----------------------------------------------------------
    def _attempt(
        self,
        task_id: str,
        lease_id: str,
        evidence: TaskEvidence,
        files: List[Dict[str, Any]],
    ) -> Tuple[bool, bool]:
        """返回 (全部成功, 是否永久失败)。"""

        for index, payload in enumerate(files):
            name = str(payload.get("filename") or "")
            try:
                from .models import EvidenceRecord

                record = EvidenceRecord(
                    filename=name,
                    size=int(payload.get("size") or 0),
                    sha256=str(payload.get("sha256") or ""),
                    kind=str(payload.get("kind") or "other"),
                    content_type=str(payload.get("content_type") or "application/octet-stream"),
                    stage=str(payload.get("stage") or "execute"),
                    collected_at=str(payload.get("collected_at") or ""),
                )
                data = evidence.file_path(name).read_bytes()
            except Exception as exc:
                log.warning("证据文件不可读 %s/%s: %s", task_id, name, exc)
                return False, True

            response: Response = self.client.evidence(
                task_id,
                lease_id,
                record,
                data,
                manifest_index=index,
            )
            if response.ok:
                continue
            if response.status == 409 and response.error_code in ("evidence_exists", ""):
                continue
            if response.status in (400, 413, 404, 410):
                log.warning("证据上传永久失败 %s/%s status=%s", task_id, name, response.status)
                return False, True
            log.warning("证据上传失败 %s/%s status=%s，稍后重试", task_id, name, response.status)
            return False, False
        return True, False

    def _update(self, task_id: str, status: str, manifest: list, incomplete: bool) -> None:
        if self.reporter is not None:
            self.reporter.update_evidence(task_id, status, manifest, incomplete)


__all__ = ["EVIDENCE_DELAYS", "EvidenceUploader", "MAX_ATTEMPTS"]
