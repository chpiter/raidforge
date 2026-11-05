"""Read-only evidence session API tests (HTTP uploads)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docx import Document

SESSION_ID = "SN1_20261104T100000Z"


def _write_session(
    settings,
    *,
    with_report: bool = True,
    session_id: str = SESSION_ID,
    server_sn: str = "SN1",
) -> Path:
    root = Path(settings.session_dir)
    session = root / session_id
    (session / "snapshots").mkdir(parents=True, exist_ok=True)
    (session / "controller_raw").mkdir(parents=True, exist_ok=True)

    events = [
        {
            "seq": 1,
            "ts": "2026-11-04T10:00:00Z",
            "event": "session.started",
            "server_sn": server_sn,
            "session_id": session_id,
            "message": "started",
        },
        {
            "seq": 2,
            "ts": "2026-11-04T10:01:00Z",
            "event": "erasure.submit",
            "server_sn": server_sn,
            "session_id": session_id,
            "message": "submitted",
            "phase": "execute",
        },
    ]
    (session / "events.jsonl").write_text(
        "\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8"
    )
    (session / "snapshots" / "01-dashboard.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><text>dashboard</text></svg>',
        encoding="utf-8",
    )
    (session / "controller_raw" / "storcli.txt").write_text("storcli output", encoding="utf-8")
    (session / "session.cast").write_text('{"version": 2}\n', encoding="utf-8")
    (session / "binary.bin").write_bytes(b"\x00\x01\x02")

    report = session / f"{server_sn}-raidforge-report-20261104.docx"
    if with_report:
        document = Document()
        document.add_heading("一、服务器信息", level=1)
        document.add_paragraph("<script>alert(1)</script>")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "序列号"
        table.rows[0].cells[1].text = "SN1"
        document.save(report)

    manifest = {
        "schema": "raidforge-evidence/manifest@2",
        "report_file": report.name if with_report else "",
        "files": [{"path": "events.jsonl", "sha256": "a" * 64, "size": 1}],
    }
    (session / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = {
        "session_id": session_id,
        "server_sn": server_sn,
        "received_at": "2026-11-04T10:02:00Z",
        "bytes": 12345,
        "sha256": "b" * 64,
        "events": len(events),
        "last_event": "erasure.submit",
        "path": str(session),
    }
    (session / "_receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (root / f"{session_id}.tar.gz").write_bytes(b"archive")
    return session


def test_list_and_search_sessions(client, settings):
    _write_session(settings)
    response = client.get("/api/cp/sessions")
    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["session_id"] == SESSION_ID
    assert item["server_sn"] == "SN1"
    assert item["report_file"].endswith(".docx")
    assert item["file_count"] >= 5

    matched = client.get("/api/cp/sessions", query_string={"q": "SN1"})
    assert matched.get_json()["total"] == 1
    missing = client.get("/api/cp/sessions", query_string={"q": "nope"})
    assert missing.get_json()["total"] == 0


def test_sessions_has_report_filter(client, settings):
    _write_session(settings)
    _write_session(
        settings,
        with_report=False,
        session_id="SN2_20261104T110000Z",
        server_sn="SN2",
    )

    assert client.get("/api/cp/sessions").get_json()["total"] == 2

    filtered = client.get("/api/cp/sessions?has_report=1").get_json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["report_file"].endswith(".docx")

    assert client.get("/api/cp/sessions?has_report=0").get_json()["total"] == 2

    invalid = client.get("/api/cp/sessions?has_report=maybe")
    assert invalid.status_code == 400
    assert invalid.get_json()["error_code"] == "invalid_params"


def test_session_detail_lists_files_and_report(client, settings):
    _write_session(settings)
    response = client.get(f"/api/cp/sessions/{SESSION_ID}")
    assert response.status_code == 200
    session = response.get_json()["session"]
    assert session["server_sn"] == "SN1"
    assert session["file_count"] >= 5
    assert session["report"]["name"].endswith(".docx")
    paths = {item["path"] for item in session["files"]}
    assert "events.jsonl" in paths
    assert "snapshots/01-dashboard.svg" in paths
    receipt_path = next(item for item in session["files"] if item["path"] == "_receipt.json")
    assert receipt_path["path"] == "_receipt.json"


def test_file_preview_and_mime_types(client, settings):
    _write_session(settings)
    text = client.get(
        f"/api/cp/sessions/{SESSION_ID}/file",
        query_string={"path": "controller_raw/storcli.txt"},
    )
    assert text.status_code == 200
    assert text.get_data(as_text=True) == "storcli output"
    assert text.mimetype == "text/plain"

    svg = client.get(
        f"/api/cp/sessions/{SESSION_ID}/file",
        query_string={"path": "snapshots/01-dashboard.svg"},
    )
    assert svg.status_code == 200
    assert svg.mimetype == "image/svg+xml"
    assert svg.headers["X-Content-Type-Options"] == "nosniff"

    binary = client.get(
        f"/api/cp/sessions/{SESSION_ID}/file",
        query_string={"path": "binary.bin"},
    )
    assert binary.status_code == 200
    assert "attachment" in binary.headers.get("Content-Disposition", "")


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", ".hidden"])
def test_file_path_traversal_is_rejected(client, settings, path):
    _write_session(settings)
    response = client.get(
        f"/api/cp/sessions/{SESSION_ID}/file",
        query_string={"path": path},
    )
    assert response.status_code in (400, 404)


def test_report_html_is_escaped(client, settings):
    _write_session(settings)
    response = client.get(f"/api/cp/sessions/{SESSION_ID}/report")
    assert response.status_code == 200
    body = response.get_json()
    assert body["filename"].endswith(".docx")
    assert "一、服务器信息" in body["html"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body["html"]
    assert "<script>" not in body["html"]
    assert "rf-report-table" in body["html"]
    assert "SN1" in body["html"]


def test_missing_report_and_session(client, settings):
    _write_session(settings, with_report=False)
    report = client.get(f"/api/cp/sessions/{SESSION_ID}/report")
    assert report.status_code == 404
    assert report.get_json()["error_code"] == "report_not_found"

    missing = client.get("/api/cp/sessions/DOES-NOT-EXIST")
    assert missing.status_code == 404


def test_events_and_archive(client, settings):
    _write_session(settings)
    events = client.get(f"/api/cp/sessions/{SESSION_ID}/events")
    assert events.status_code == 200
    items = events.get_json()["items"]
    assert items[0]["kind"] == "session.started"
    assert items[1]["phase"] == "execute"

    archive = client.get(f"/api/cp/sessions/{SESSION_ID}/download")
    assert archive.status_code == 200
    assert "attachment" in archive.headers.get("Content-Disposition", "")
