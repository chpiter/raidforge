"""HTTP 客户端：每线程独立 Session、固定请求头、重试与脱敏。"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple
from urllib.parse import quote

from .clock import Clock, SystemClock
from .config import AgentConfig
from .models import AgentResult, EvidenceRecord
from .version import AGENT_VERSION, PROTOCOL_VERSION

try:  # 缺少 requests 时 Agent 进入未配置/不可用，但 TUI 仍可启动
    import requests
except Exception:  # pragma: no cover - 仅在极端裁剪环境
    requests = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

CONTROL_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_CONTROL_ATTEMPTS = 5


@dataclass
class Response:
    status: int = 0
    body: Optional[Dict[str, Any]] = None
    text: str = ""
    error: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error_code(self) -> str:
        if isinstance(self.body, dict):
            return str(self.body.get("error_code") or "")
        return ""

    @property
    def retryable(self) -> bool:
        if self.status == 0:
            return True
        return self.status in TRANSIENT_STATUS

    @property
    def error_message(self) -> str:
        if isinstance(self.body, dict):
            return str(self.body.get("error") or "")
        return self.error or self.text[:200]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "error_code": self.error_code,
            "error": self.error_message,
        }


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: Tuple[float, float],
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[Mapping[str, str]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> Response: ...


class RequestsTransport:
    """每个线程一个 ``requests.Session``；不复用、不共享。"""

    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self._local = threading.local()
        self.verify = cfg.ca_bundle or True

    def _session(self):
        if requests is None:
            raise RuntimeError("requests 未安装")
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            self._local.session = session
        return session

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: Tuple[float, float],
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[Mapping[str, str]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> Response:
        started = time.monotonic()
        try:
            session = self._session()
            result = session.request(
                method,
                url,
                headers=dict(headers),
                timeout=timeout,
                json=json_body,
                data=data,
                files=files,
                verify=self.verify,
                allow_redirects=False,
            )
        except Exception as exc:
            log.warning("请求失败 %s %s: %s", method, _safe_path(url), type(exc).__name__)
            return Response(status=0, error=f"{type(exc).__name__}: {exc}")

        duration = time.monotonic() - started
        body: Optional[Dict[str, Any]] = None
        text = ""
        try:
            text = result.text or ""
        except Exception:
            text = ""
        if text:
            try:
                import json

                parsed = json.loads(text)
                body = parsed if isinstance(parsed, dict) else None
            except ValueError:
                body = None
        log.info(
            "%s %s -> %s %.3fs",
            method,
            _safe_path(url),
            result.status_code,
            duration,
        )
        return Response(
            status=int(result.status_code),
            body=body,
            text=text,
            headers={str(key).lower(): str(value) for key, value in result.headers.items()},
        )


def _safe_path(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        return parts.path or "/"
    except Exception:
        return "/"


class AgentClient:
    def __init__(
        self,
        cfg: AgentConfig,
        transport: Optional[HttpTransport] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.transport = transport or RequestsTransport(cfg)

    # -- headers ------------------------------------------------------------
    def headers(self, *, json_body: bool = False, idempotency_key: str = "") -> Dict[str, str]:
        headers = {
            "X-Raidforge-Token": self.cfg.token,
            "X-Raidforge-Agent-Id": self.cfg.agent_id,
            "X-Raidforge-Agent-Version": AGENT_VERSION,
            "X-Raidforge-Protocol": str(PROTOCOL_VERSION),
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _url(self, path: str) -> str:
        return f"{self.cfg.base_url}{path}"

    # -- core request -------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[Mapping[str, str]] = None,
        files: Optional[Dict[str, Any]] = None,
        timeout: Tuple[float, float] = (5.0, 30.0),
        idempotency_key: str = "",
        attempts: int = 1,
    ) -> Response:
        headers = self.headers(json_body=json_body is not None, idempotency_key=idempotency_key)
        url = self._url(path)
        attempt = 0
        last = Response(status=0, error="not attempted")
        while attempt < max(1, attempts):
            attempt += 1
            last = self.transport.request(
                method,
                url,
                headers=headers,
                timeout=timeout,
                json_body=json_body,
                data=data,
                files=files,
            )
            if not last.retryable or attempt >= attempts:
                return last
            delay = CONTROL_DELAYS[min(attempt - 1, len(CONTROL_DELAYS) - 1)]
            if last.status == 429:
                retry_after = last.headers.get("retry-after", "")
                try:
                    delay = max(delay, float(retry_after))
                except (TypeError, ValueError):
                    pass
            delay += random.uniform(0.0, 0.5)
            self.clock.sleep(delay)
        return last

    # -- endpoints ----------------------------------------------------------
    def heartbeat(self, payload: Dict[str, Any]) -> Response:
        return self.request(
            "POST",
            "/agent/heartbeat",
            json_body=payload,
            timeout=(3.05, 5.0),
            attempts=MAX_CONTROL_ATTEMPTS,
        )

    def next_task(self, server_sn: str) -> Response:
        return self.request(
            "GET",
            f"/agent/tasks/next?server_sn={quote(server_sn)}",
            timeout=(3.05, 10.0),
            attempts=MAX_CONTROL_ATTEMPTS,
        )

    def get_task(self, task_id: str) -> Response:
        return self.request(
            "GET",
            f"/agent/tasks/{quote(task_id)}",
            timeout=(5.0, 30.0),
            attempts=MAX_CONTROL_ATTEMPTS,
        )

    def started(self, task_id: str, lease_id: str) -> Response:
        return self.request(
            "POST",
            f"/agent/tasks/{quote(task_id)}/started",
            json_body={
                "schema_version": 1,
                "agent_id": self.cfg.agent_id,
                "lease_id": lease_id,
            },
            timeout=(5.0, 30.0),
            attempts=MAX_CONTROL_ATTEMPTS,
        )

    def progress(self, task_id: str, lease_id: str, payload: Dict[str, Any]) -> Response:
        body = {
            "schema_version": 1,
            "agent_id": self.cfg.agent_id,
            "lease_id": lease_id,
            **payload,
        }
        return self.request(
            "POST",
            f"/agent/tasks/{quote(task_id)}/progress",
            json_body=body,
            timeout=(5.0, 30.0),
            attempts=1,
        )

    def cancel_requested(self, task_id: str) -> Response:
        return self.request(
            "GET",
            f"/agent/tasks/{quote(task_id)}/cancel_requested",
            timeout=(5.0, 10.0),
            attempts=1,
        )

    def result(self, task_id: str, payload: Dict[str, Any], attempts: int = 1) -> Response:
        return self.request(
            "POST",
            f"/agent/tasks/{quote(task_id)}/result",
            json_body=payload,
            timeout=(5.0, 30.0),
            idempotency_key=f"{task_id}:{payload.get('lease_id', '')}",
            attempts=attempts,
        )

    def evidence(
        self,
        task_id: str,
        lease_id: str,
        record: EvidenceRecord,
        data: bytes,
        *,
        manifest_index: int = 0,
    ) -> Response:
        fields = {
            "sha256": record.sha256,
            "size": str(record.size),
            "kind": record.kind,
            "stage": record.stage,
            "collected_at": record.collected_at,
            "lease_id": lease_id,
            "content_type": record.content_type,
            "manifest_index": str(manifest_index),
        }
        files = {
            "file": (
                record.filename,
                data,
                record.content_type or "application/octet-stream",
            )
        }
        return self.request(
            "POST",
            f"/agent/tasks/{quote(task_id)}/evidence",
            data=fields,
            files=files,
            timeout=(10.0, 300.0),
            attempts=1,
        )

    def release(self, task_id: str, lease_id: str, reason: str = "") -> Response:
        return self.request(
            "POST",
            f"/agent/tasks/{quote(task_id)}/release",
            json_body={
                "schema_version": 1,
                "agent_id": self.cfg.agent_id,
                "lease_id": lease_id,
                "reason": reason,
            },
            timeout=(5.0, 30.0),
            attempts=2,
        )

    # -- helpers ------------------------------------------------------------
    def scrub(self, text: str) -> str:
        token = self.cfg.token
        if token and token in text:
            return text.replace(token, "***")
        return text


__all__ = [
    "AgentClient",
    "CONTROL_DELAYS",
    "HttpTransport",
    "RequestsTransport",
    "Response",
    "TRANSIENT_STATUS",
]
