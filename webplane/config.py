"""Runtime settings for the RaidForge control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = Path("webplane-data")
    db_path: Optional[Path] = None
    evidence_dir: Optional[Path] = None
    agent_token: str = ""
    protocol_version: int = 1
    lease_seconds: int = 900
    task_ttl_seconds: int = 3600
    agent_offline_seconds: int = 45
    max_evidence_bytes: int = 64 * 1024 * 1024
    max_task_evidence_bytes: int = 512 * 1024 * 1024
    heartbeat_interval: int = 10
    poll_interval: int = 3
    static_dir: Optional[Path] = None
    host: str = "0.0.0.0"
    port: int = 8080
    maintenance: bool = True
    max_task_attempts: int = 3
    maintenance_interval: int = 15

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.db_path = (
            Path(self.db_path).expanduser().resolve()
            if self.db_path
            else self.data_dir / "webplane.sqlite3"
        )
        self.evidence_dir = (
            Path(self.evidence_dir).expanduser().resolve()
            if self.evidence_dir
            else self.data_dir / "evidence"
        )
        if self.static_dir is not None:
            self.static_dir = Path(self.static_dir).expanduser().resolve()
        else:
            self.static_dir = Path(__file__).resolve().parent / "web" / "dist"
        self.lease_seconds = min(7200, max(60, int(self.lease_seconds)))
        self.task_ttl_seconds = min(86400, max(60, int(self.task_ttl_seconds)))
        self.agent_offline_seconds = max(5, int(self.agent_offline_seconds))
        self.max_evidence_bytes = max(1, int(self.max_evidence_bytes))
        self.max_task_evidence_bytes = max(self.max_evidence_bytes, int(self.max_task_evidence_bytes))
        self.heartbeat_interval = min(600, max(1, int(self.heartbeat_interval)))
        self.poll_interval = min(300, max(1, int(self.poll_interval)))
        self.max_task_attempts = max(1, int(self.max_task_attempts))
        self.maintenance_interval = max(5, int(self.maintenance_interval))

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("WEBPLANE_AGENT_TOKEN", "") or os.environ.get(
            "RAIDFORGE_CP_TOKEN", ""
        )
        static_raw = os.environ.get("WEBPLANE_STATIC_DIR", "").strip()
        return cls(
            data_dir=Path(os.environ.get("WEBPLANE_DATA_DIR", "webplane-data")),
            db_path=Path(os.environ["WEBPLANE_DB"]) if os.environ.get("WEBPLANE_DB") else None,
            evidence_dir=Path(os.environ["WEBPLANE_EVIDENCE_DIR"])
            if os.environ.get("WEBPLANE_EVIDENCE_DIR")
            else None,
            agent_token=token.strip(),
            protocol_version=_env_int("WEBPLANE_PROTOCOL_VERSION", 1),
            lease_seconds=_env_int("WEBPLANE_LEASE_SECONDS", 900),
            task_ttl_seconds=_env_int("WEBPLANE_TASK_TTL_SECONDS", 3600),
            agent_offline_seconds=_env_int("WEBPLANE_AGENT_OFFLINE_SECONDS", 45),
            max_evidence_bytes=_env_int("WEBPLANE_MAX_EVIDENCE_BYTES", 64 * 1024 * 1024),
            max_task_evidence_bytes=_env_int(
                "WEBPLANE_MAX_TASK_EVIDENCE_BYTES", 512 * 1024 * 1024
            ),
            heartbeat_interval=_env_int("WEBPLANE_HEARTBEAT_INTERVAL", 10),
            poll_interval=_env_int("WEBPLANE_POLL_INTERVAL", 3),
            static_dir=Path(static_raw) if static_raw else None,
            host=os.environ.get("WEBPLANE_HOST", "0.0.0.0"),
            port=_env_int("WEBPLANE_PORT", 8080),
            maintenance=_env_bool("WEBPLANE_MAINTENANCE", True),
            max_task_attempts=_env_int("WEBPLANE_MAX_TASK_ATTEMPTS", 3),
            maintenance_interval=_env_int("WEBPLANE_MAINTENANCE_INTERVAL", 15),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)


__all__ = ["Settings"]
