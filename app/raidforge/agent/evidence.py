"""任务级证据目录、流式哈希与 manifest。"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .clock import Clock, SystemClock, rfc3339
from .models import EvidenceRecord
from .state import atomic_write_bytes, atomic_write_json

log = logging.getLogger(__name__)

EVIDENCE_SCHEMA = "raidforge-agent-evidence@1"
KINDS = {
    "command_output",
    "inventory",
    "plan",
    "screenshot",
    "report",
    "log",
    "json",
    "other",
}
STAGES = {"preflight", "confirm", "execute", "postcheck", "cleanup"}
ALLOWED_EXTENSIONS = {".json", ".txt", ".log", ".csv", ".png", ".jpg", ".gz"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MIN_FREE_BYTES = 512 * 1024 * 1024
_MAGIC = {
    ".png": b"\x89PNG",
    ".jpg": b"\xff\xd8\xff",
    ".gz": b"\x1f\x8b",
}


class EvidenceLimitExceeded(Exception):
    pass


@dataclass
class TaskEvidence:
    evidence_root: Path
    task_id: str
    clock: Optional[Clock] = None
    max_files: int = 32
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    root: Path = field(init=False)
    manifest_path: Path = field(init=False)
    events_path: Path = field(init=False)
    _entries: Dict[str, Dict[str, Any]] = field(default_factory=dict, init=False)
    _seq: int = field(default=0, init=False)
    _total_bytes: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.clock = self.clock or SystemClock()
        safe_task = "".join(char for char in self.task_id if char.isalnum() or char in "._-")
        self.root = Path(self.evidence_root) / safe_task
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.events_path = self.root / "events.jsonl"

    # -- capability ---------------------------------------------------------
    @property
    def limit_bytes(self) -> int:
        return self.max_total_bytes

    def free_bytes(self) -> int:
        try:
            return int(shutil.disk_usage(str(self.root)).free)
        except OSError:
            return 0

    def check_space(self) -> bool:
        return self.free_bytes() >= MIN_FREE_BYTES

    # -- writers ------------------------------------------------------------
    def write_json(
        self,
        name: str,
        payload: Any,
        *,
        kind: str = "json",
        stage: str = "execute",
        content_type: str = "application/json",
    ) -> EvidenceRecord:
        data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        return self.write_bytes(name, data, kind=kind, stage=stage, content_type=content_type)

    def write_text(
        self,
        name: str,
        text: str,
        *,
        kind: str = "log",
        stage: str = "execute",
        content_type: str = "text/plain",
    ) -> EvidenceRecord:
        return self.write_bytes(name, (text or "").encode("utf-8"), kind=kind, stage=stage, content_type=content_type)

    def copy_in(
        self,
        name: str,
        source: Path,
        *,
        kind: str = "other",
        stage: str = "postcheck",
        content_type: str = "",
    ) -> EvidenceRecord:
        data = Path(source).read_bytes()
        return self.write_bytes(
            name,
            data,
            kind=kind,
            stage=stage,
            content_type=content_type or _guess_type(name),
        )

    def write_bytes(
        self,
        name: str,
        data: bytes,
        *,
        kind: str,
        stage: str,
        content_type: str = "",
    ) -> EvidenceRecord:
        filename = self._validate_name(name)
        if kind not in KINDS:
            raise EvidenceLimitExceeded(f"evidence kind {kind!r} 不支持")
        if stage not in STAGES:
            raise EvidenceLimitExceeded(f"evidence stage {stage!r} 不支持")
        if len(data) > self.max_file_bytes:
            raise EvidenceLimitExceeded(f"{filename} 超过单文件上限")
        if len(self._entries) + 1 > self.max_files:
            raise EvidenceLimitExceeded("证据文件数超过上限")
        if self._total_bytes + len(data) > self.max_total_bytes:
            raise EvidenceLimitExceeded("证据总量超过上限")
        self._check_magic(filename, data)

        target = self._safe_path(filename)
        temp = target.with_name(f".{target.name}.part")
        digest = hashlib.sha256()
        try:
            with temp.open("wb") as handle:
                for offset in range(0, len(data), 1024 * 1024):
                    chunk = data[offset : offset + 1024 * 1024]
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp), str(target))
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass

        record = EvidenceRecord(
            filename=filename,
            size=len(data),
            sha256=digest.hexdigest(),
            kind=kind,
            content_type=content_type or _guess_type(filename),
            stage=stage,
            collected_at=rfc3339(self.clock.utcnow()),
        )
        self._entries[filename] = record.to_dict()
        self._total_bytes += len(data)
        return record

    # -- audit events -------------------------------------------------------
    def emit(self, event: str, **payload: Any) -> Dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": rfc3339(self.clock.utcnow()),
            "event": event,
            "task_id": self.task_id,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        return record

    # -- manifest -----------------------------------------------------------
    def manifest(self) -> Dict[str, Any]:
        files = [self._entries[name] for name in sorted(self._entries)]
        return {
            "schema": EVIDENCE_SCHEMA,
            "task_id": self.task_id,
            "file_count": len(files),
            "total_bytes": self._total_bytes,
            "files": files,
            "truncated": False,
        }

    def records(self) -> List[EvidenceRecord]:
        return [EvidenceRecord(**payload) for _, payload in sorted(self._entries.items())]

    def finalize(self) -> Dict[str, Any]:
        payload = self.manifest()
        atomic_write_json(self.manifest_path, payload, mode=0o400)
        try:
            os.chmod(self.manifest_path, 0o400)
        except OSError:
            pass
        for name in self._entries:
            try:
                os.chmod(self._safe_path(name), 0o400)
            except OSError:
                pass
        try:
            os.chmod(self.root, 0o500)
        except OSError:
            pass
        return payload

    def set_partial(self, partial: bool = True) -> None:
        payload = self.manifest()
        payload["truncated"] = bool(partial)
        try:
            os.chmod(self.manifest_path, 0o600)
        except OSError:
            pass
        atomic_write_json(self.manifest_path, payload, mode=0o600)

    def file_path(self, name: str) -> Path:
        return self._safe_path(self._validate_name(name))

    # -- internals ----------------------------------------------------------
    def _safe_path(self, name: str) -> Path:
        target = (self.root / name).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            raise EvidenceLimitExceeded(f"证据路径越界: {name}")
        return target

    def _validate_name(self, name: str) -> str:
        filename = str(name or "").strip()
        if not _NAME_RE.match(filename):
            raise EvidenceLimitExceeded(f"证据文件名非法: {filename!r}")
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise EvidenceLimitExceeded(f"证据扩展名不允许: {suffix}")
        return filename

    def _check_magic(self, filename: str, data: bytes) -> None:
        suffix = Path(filename).suffix.lower()
        magic = _MAGIC.get(suffix)
        if magic and not data.startswith(magic):
            raise EvidenceLimitExceeded(f"{filename} magic-byte 不匹配")
        if suffix == ".json":
            head = data.lstrip()[:1]
            if head not in (b"{", b"["):
                raise EvidenceLimitExceeded(f"{filename} 不是合法 JSON")


def _guess_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def gc_evidence(root: Path, retention_days: int = 7, now: Optional[float] = None) -> List[Path]:
    """删除超过保留期的任务目录；路径必须先确认在 root 内。"""

    root_path = Path(root).resolve()
    cutoff = (now or time.time()) - retention_days * 24 * 3600
    removed: List[Path] = []
    try:
        entries = list(root_path.iterdir())
    except OSError:
        return removed
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            resolved = entry.resolve()
            if root_path not in resolved.parents:
                continue
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
        except OSError:
            continue
    return removed


__all__ = [
    "ALLOWED_EXTENSIONS",
    "EVIDENCE_SCHEMA",
    "EvidenceLimitExceeded",
    "KINDS",
    "MIN_FREE_BYTES",
    "STAGES",
    "TaskEvidence",
    "gc_evidence",
]
