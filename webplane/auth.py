"""Agent request guards and shared API error type."""

from __future__ import annotations

import hmac
from typing import Any, Dict, Optional

from flask import jsonify, request


class ApiError(Exception):
    status = 400
    code = "bad_request"

    def __init__(
        self,
        message: str = "",
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> None:
        super().__init__(message or code or self.code)
        self.message = message or code or self.code
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.fields = list(fields or [])


def error_response(exc: ApiError):
    payload: Dict[str, Any] = {"error_code": exc.code, "message": exc.message}
    if exc.fields:
        payload["fields"] = exc.fields
    return jsonify(payload), exc.status


def header(name: str) -> str:
    return (request.headers.get(name) or "").strip()


def agent_id_from_request(payload: Optional[Dict[str, Any]] = None) -> str:
    value = header("X-Raidforge-Agent-Id")
    if not value and payload:
        value = str(payload.get("agent_id") or "").strip()
    if len(value) > 128:
        raise ApiError("agent_id 过长", code="invalid_agent_id", status=400)
    return value


def lease_from_request(payload: Optional[Dict[str, Any]] = None) -> str:
    value = header("X-Raidforge-Lease")
    if not value and payload:
        value = str(payload.get("lease_id") or "").strip()
    if not value:
        value = (request.args.get("lease_id") or "").strip()
    return value


def check_protocol(settings) -> None:
    raw = header("X-Raidforge-Protocol") or header("X-Protocol")
    if not raw:
        return
    try:
        version = int(raw)
    except ValueError:
        raise ApiError("协议版本非法", code="protocol_too_old", status=426) from None
    if version != int(settings.protocol_version):
        raise ApiError(
            f"服务端协议版本为 {settings.protocol_version}",
            code="protocol_too_old",
            status=426,
        )


def check_token(settings) -> None:
    expected = settings.agent_token
    if not expected:
        return
    supplied = header("X-Raidforge-Token")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise ApiError("invalid or missing token", code="auth_failed", status=401)


def guard_agent_request(settings) -> None:
    check_protocol(settings)
    check_token(settings)


__all__ = [
    "ApiError",
    "agent_id_from_request",
    "check_protocol",
    "check_token",
    "error_response",
    "guard_agent_request",
    "header",
    "lease_from_request",
]
