"""Read-only access to HTTP-uploaded evidence sessions.

Sessions are written by :mod:`webplane.api.evidence_api` as::

    WEBPLANE_SESSION_DIR/<session_id>/
        manifest.json
        events.jsonl
        snapshots/
        controller_raw/
        <serial>-raidforge-report-<date>.docx

This module never mutates a session.  It resolves paths defensively, lists
files, normalises events for the Vue timeline and renders DOCX reports to
escaped HTML.
"""

from __future__ import annotations

import html
import json
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..auth import ApiError

PREVIEW_SUFFIXES = {
    ".json",
    ".jsonl",
    ".txt",
    ".log",
    ".csv",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".cast",
}
IMAGE_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg"}
REPORT_SUFFIX = ".docx"
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 2000


def session_root(settings) -> Path:
    root = Path(settings.session_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def safe_session_id(value: str) -> str:
    candidate = Path(str(value or "").strip()).name
    cleaned = "".join(ch for ch in candidate if ch.isalnum() or ch in "._-")
    if not cleaned or cleaned in {".", ".."}:
        raise ApiError("会话 ID 非法", code="invalid_session_id", status=400)
    return cleaned[:128]


def resolve_session(settings, session_id: str) -> Path:
    root = session_root(settings)
    path = (root / safe_session_id(session_id)).resolve()
    if not path.is_relative_to(root) or not path.is_dir() or path.is_symlink():
        raise ApiError("会话不存在", code="session_not_found", status=404)
    return path


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_receipt(session_dir: Path) -> Dict[str, Any]:
    payload = _read_json(session_dir / "_receipt.json")
    payload.pop("path", None)
    return payload


def read_manifest(session_dir: Path) -> Dict[str, Any]:
    return _read_json(session_dir / "manifest.json")


def _manifest_hashes(manifest: Dict[str, Any]) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    files = manifest.get("files")
    if not isinstance(files, list):
        return hashes
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        digest = str(item.get("sha256") or "").strip()
        if path and digest:
            hashes[path] = digest
    return hashes


def _report_path(session_dir: Path, manifest: Dict[str, Any]) -> Optional[Path]:
    named = str(manifest.get("report_file") or "").strip()
    if named:
        try:
            candidate = resolve_file(session_dir, named)
        except ApiError:
            candidate = None
        if candidate is not None and candidate.suffix.lower() == REPORT_SUFFIX:
            return candidate
    for candidate in sorted(session_dir.glob(f"*-raidforge-report-*{REPORT_SUFFIX}")):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    return None


def _is_hidden(relative: Path) -> bool:
    return any(part.startswith(".") for part in relative.parts)


def list_files(session_dir: Path) -> List[Dict[str, Any]]:
    root = session_dir.resolve()
    manifest = read_manifest(session_dir)
    hashes = _manifest_hashes(manifest)
    report = _report_path(session_dir, manifest)
    items: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if _is_hidden(relative):
            continue
        rel = relative.as_posix()
        suffix = path.suffix.lower()
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        items.append(
            {
                "path": rel,
                "name": path.name,
                "size": size,
                "sha256": hashes.get(rel, ""),
                "content_type": content_type,
                "previewable": suffix in PREVIEW_SUFFIXES and size <= MAX_PREVIEW_BYTES,
                "is_report": report is not None and path.resolve() == report,
                "kind": "report" if report is not None and path.resolve() == report else "file",
            }
        )
    return items


def _report_info(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for item in files:
        if item["is_report"]:
            return {
                "path": item["path"],
                "name": item["name"],
                "size": item["size"],
                "sha256": item["sha256"],
            }
    return None


def get_session(settings, session_id: str) -> Dict[str, Any]:
    session_dir = resolve_session(settings, session_id)
    receipt = read_receipt(session_dir)
    manifest = read_manifest(session_dir)
    files = list_files(session_dir)
    return {
        "session_id": receipt.get("session_id") or session_dir.name,
        "server_sn": receipt.get("server_sn", ""),
        "received_at": receipt.get("received_at", ""),
        "bytes": receipt.get("bytes", 0),
        "sha256": receipt.get("sha256", ""),
        "events": receipt.get("events", 0),
        "last_event": receipt.get("last_event", ""),
        "file_count": len(files),
        "report": _report_info(files),
        "manifest": manifest,
        "files": files,
    }


def list_sessions(
    settings,
    page: int = 1,
    page_size: int = 20,
    query: str = "",
    has_report: bool = False,
) -> Tuple[List[Dict[str, Any]], int]:
    root = session_root(settings)
    needle = query.strip().lower()
    records: List[Dict[str, Any]] = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
            continue
        receipt = read_receipt(directory)
        manifest = read_manifest(directory)
        report = _report_path(directory, manifest)
        record = {
            "session_id": receipt.get("session_id") or directory.name,
            "server_sn": receipt.get("server_sn", ""),
            "received_at": receipt.get("received_at", ""),
            "bytes": receipt.get("bytes", 0),
            "sha256": receipt.get("sha256", ""),
            "events": receipt.get("events", 0),
            "last_event": receipt.get("last_event", ""),
            "file_count": sum(
                1
                for path in directory.rglob("*")
                if path.is_file() and not path.is_symlink() and not _is_hidden(path.relative_to(directory))
            ),
            "report_file": report.name if report is not None else "",
        }
        if has_report and not record["report_file"]:
            continue
        if needle:
            haystack = " ".join(
                [record["session_id"], record["server_sn"], record["last_event"]]
            ).lower()
            if needle not in haystack:
                continue
        records.append(record)
    records.sort(key=lambda item: (item["received_at"], item["session_id"]), reverse=True)
    total = len(records)
    start = max(0, (page - 1) * page_size)
    return records[start : start + page_size], total


def read_events(session_dir: Path, limit: int = 500) -> List[Dict[str, Any]]:
    path = session_dir / "events.jsonl"
    if not path.is_file():
        return []
    items: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        data = {
            key: value
            for key, value in record.items()
            if key not in {"seq", "ts", "event", "server_sn", "session_id"}
        }
        event_name = str(record.get("event") or "event")
        items.append(
            {
                "id": record.get("seq") or index,
                "created_at": str(record.get("ts") or ""),
                "kind": event_name,
                "phase": str(data.get("phase") or ""),
                "actor": str(data.get("actor") or "agent"),
                "message": str(data.get("message") or event_name),
                "data": data,
            }
        )
        if len(items) >= max(1, min(limit, MAX_EVENTS)):
            break
    return items


def resolve_file(session_dir: Path, rel_path: str) -> Path:
    text = str(rel_path or "").strip().replace("\\", "/")
    if not text:
        raise ApiError("缺少文件路径", code="invalid_path", status=400)
    root = session_dir.resolve()
    candidate = (root / text).resolve()
    if not candidate.is_relative_to(root):
        raise ApiError("文件路径非法", code="invalid_path", status=400)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ApiError("文件路径非法", code="invalid_path", status=400) from None
    if _is_hidden(relative):
        raise ApiError("文件路径非法", code="invalid_path", status=400)
    if candidate.is_symlink() or not candidate.is_file():
        raise ApiError("文件不存在", code="file_not_found", status=404)
    return candidate


def content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def read_text_preview(path: Path, limit: int = MAX_PREVIEW_BYTES) -> str:
    try:
        data = path.read_bytes()[:limit]
    except OSError:
        raise ApiError("文件不存在", code="file_not_found", status=404) from None
    return data.decode("utf-8", errors="replace")


def _run_html(run) -> str:
    text = html.escape(str(run.text or ""))
    if not text:
        return ""
    if run.bold:
        text = f"<strong>{text}</strong>"
    if run.italic:
        text = f"<em>{text}</em>"
    return text


def _paragraph_html(paragraph) -> str:
    inner = "".join(_run_html(run) for run in paragraph.runs)
    if not inner.strip():
        return ""
    style = (paragraph.style.name if paragraph.style else "") or ""
    lowered = style.lower()
    if lowered.startswith("heading"):
        level = 1
        for token in style.split():
            if token.isdigit():
                level = max(1, min(3, int(token)))
                break
        return f"<h{level}>{inner}</h{level}>"
    if lowered == "title":
        return f"<h1>{inner}</h1>"
    if lowered == "subtitle":
        return f'<p class="rf-report-subtitle">{inner}</p>'
    return f"<p>{inner}</p>"


def _table_html(table) -> str:
    rows: List[str] = []
    for row in table.rows:
        cells = "".join(f"<td>{html.escape(cell.text or '')}</td>" for cell in row.cells)
        rows.append(f"<tr>{cells}</tr>")
    if not rows:
        return ""
    return '<table class="rf-report-table">' + "".join(rows) + "</table>"


def docx_to_html(path: Path) -> str:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:  # pragma: no cover - dependency guard
        raise ApiError(
            "DOCX 预览组件未安装",
            code="preview_unavailable",
            status=503,
        ) from None

    try:
        document = Document(str(path))
    except Exception as exc:  # pragma: no cover - corrupt report
        raise ApiError(
            f"报告解析失败: {type(exc).__name__}",
            code="report_parse_failed",
            status=500,
        ) from None

    parts: List[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            parts.append(_paragraph_html(Paragraph(child, document)))
        elif child.tag == qn("w:tbl"):
            parts.append(_table_html(Table(child, document)))
    html_body = "".join(part for part in parts if part)
    if not html_body:
        html_body = "<p>报告内容为空</p>"
    return (
        '<div class="rf-report">'
        '<p class="rf-report-note">报告内嵌图片请在“截图”页查看；原始 DOCX 可下载。</p>'
        f"{html_body}</div>"
    )


__all__ = [
    "IMAGE_SUFFIXES",
    "MAX_EVENTS",
    "MAX_PREVIEW_BYTES",
    "PREVIEW_SUFFIXES",
    "REPORT_SUFFIX",
    "content_type",
    "docx_to_html",
    "get_session",
    "list_files",
    "list_sessions",
    "read_events",
    "read_manifest",
    "read_receipt",
    "read_text_preview",
    "resolve_file",
    "resolve_session",
    "safe_session_id",
    "session_root",
]
