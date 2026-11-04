from __future__ import annotations

import io
import tarfile
from pathlib import Path


def _archive(root: str = "SID1", extra: list[str] | None = None) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entries = {
            f"{root}/events.jsonl": b'{"event":"session_started"}\n',
            f"{root}/manifest.json": b'{"files":1}',
        }
        for name in extra or []:
            entries[name] = b"x"
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def _post(client, archive, session_id: str = "SID1", server_sn: str = "SN1"):
    return client.post(
        "/sessions",
        data={
            "file": (archive, f"{session_id}.tar.gz"),
            "server_sn": server_sn,
            "session_id": session_id,
        },
        content_type="multipart/form-data",
    )


def test_post_extracts_and_flattens_single_top_level_dir(client, settings):
    response = _post(client, _archive())
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["session_id"] == "SID1"
    assert body["events"] == 1
    assert body["last_event"] == "session_started"

    root = Path(settings.session_dir) / "SID1"
    assert (root / "events.jsonl").is_file()
    assert (root / "manifest.json").is_file()
    assert (root / "_receipt.json").is_file()
    assert (Path(settings.session_dir) / "SID1.tar.gz").is_file()
    assert not (root / "SID1").exists()


def test_archive_without_top_level_dir_is_accepted(client, settings):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        data = b'{"event":"started"}\n'
        info = tarfile.TarInfo("events.jsonl")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    archive.seek(0)
    response = _post(client, archive)
    assert response.status_code == 201, response.get_json()
    assert (Path(settings.session_dir) / "SID1" / "events.jsonl").is_file()


def test_duplicate_upload_is_idempotent(client):
    first = _post(client, _archive())
    second = _post(client, _archive())
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json()["duplicate"] is True


def test_missing_file_is_rejected(client):
    response = client.post("/sessions", data={"session_id": "SID1"})
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "missing_file"


def test_path_traversal_is_rejected(client, settings):
    response = _post(client, _archive(extra=["../evil.txt"]))
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "invalid_archive"
    assert not (Path(settings.session_dir).parent / "evil.txt").exists()


def test_symlink_member_is_rejected(client):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        info = tarfile.TarInfo("SID1/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    archive.seek(0)
    response = _post(client, archive)
    assert response.status_code == 400


def test_size_limit_is_enforced(client, settings):
    settings.max_session_bytes = 10
    response = _post(client, _archive())
    assert response.status_code == 413
    assert response.get_json()["error_code"] == "session_too_large"


def test_get_sessions_lists_uploads(client):
    _post(client, _archive())
    listing = client.get("/sessions")
    assert listing.status_code == 200
    body = listing.get_json()
    assert body["count"] == 1
    assert body["sessions"][0]["session_id"] == "SID1"
