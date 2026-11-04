"""HTTP endpoint derivation, health probe and transport ordering."""

from __future__ import annotations

import io
import urllib.error
from pathlib import Path

from raidforge.config import Config
from raidforge.core import report


def _cfg(**report_values) -> Config:
    cfg = Config()
    cfg.report.ssh_host = "10.0.0.5"
    for key, value in report_values.items():
        setattr(cfg.report, key, value)
    return cfg


class _Response:
    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}') -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _install_urlopen(monkeypatch, result=None, error=None):
    calls = []

    def fake(request, timeout=None):
        calls.append((request, timeout))
        if error is not None:
            raise error
        return result or _Response()

    monkeypatch.setattr(report.urllib.request, "urlopen", fake)
    return calls


# ---------------------------------------------------------------------------
# endpoint derivation
# ---------------------------------------------------------------------------
def test_http_url_for_matrix():
    assert report.http_url_for("10.0.0.5", 8080) == "http://10.0.0.5:8080"
    assert report.http_url_for("10.0.0.5", 8080, "https", "/upload") == "https://10.0.0.5:8080/upload"
    assert report.http_url_for("10.0.0.5", 8080, "ftp", "upload") == "http://10.0.0.5:8080/upload"
    assert report.http_url_for("", 8080) == ""


def test_http_endpoint_prefers_explicit_base_and_adds_scheme():
    cfg = _cfg(http_base_url="https://collector.example/root/", http_port=8080)
    assert report.http_endpoint(cfg) == "https://collector.example/root"

    cfg = _cfg(http_base_url="10.0.0.9:9000", http_port=8080)
    assert report.http_endpoint(cfg) == "http://10.0.0.9:9000"

    cfg = _cfg(http_port=8080)
    assert report.http_endpoint(cfg) == "http://10.0.0.5:8080"

    cfg = _cfg()
    cfg.report.ssh_host = ""
    cfg.report.http_base_url = ""
    assert report.http_endpoint(cfg) == ""


def test_http_transport_defaults_to_webplane():
    cfg = _cfg(transport="http", http_host="")
    cfg.webplane.host = "192.168.40.132"
    cfg.webplane.port = 8080
    assert report.http_endpoint(cfg) == "http://192.168.40.132:8080"


def test_explicit_http_host_wins_over_webplane():
    cfg = _cfg(transport="http", http_host="evidence.local", http_port=9000)
    cfg.webplane.host = "192.168.40.132"
    cfg.webplane.port = 8080
    assert report.http_endpoint(cfg) == "http://evidence.local:9000"


def test_ssh_transport_keeps_legacy_http_fallback():
    cfg = _cfg(transport="ssh")
    assert report.http_endpoint(cfg) == "http://10.0.0.5:8080"


def test_legacy_derived_base_is_ignored_for_http_transport():
    cfg = _cfg(
        transport="http",
        http_base_url="http://10.0.0.5:8080",
        http_port=8080,
    )
    cfg.webplane.host = "1.2.3.4"
    cfg.webplane.port = 8080
    assert report.http_endpoint(cfg) == "http://1.2.3.4:8080"


def test_explicit_non_legacy_base_still_wins():
    cfg = _cfg(
        transport="http",
        http_base_url="https://collector.example/root/",
    )
    cfg.webplane.host = "1.2.3.4"
    assert report.http_endpoint(cfg) == "https://collector.example/root"


def test_ordered_transports_follows_preference():
    assert report.ordered_transports(_cfg(transport="ssh")) == ["ssh", "http"]
    assert report.ordered_transports(_cfg(transport="http")) == ["http", "ssh"]

    cfg = _cfg()
    cfg.report.transport = "telnet"
    assert report.ordered_transports(cfg) == ["ssh", "http"]


# ---------------------------------------------------------------------------
# try_http
# ---------------------------------------------------------------------------
def test_try_http_posts_to_sessions_on_derived_endpoint(tmp_path, monkeypatch):
    session = tmp_path / "SN1_20260101T000000Z"
    session.mkdir()
    (session / "events.jsonl").write_text("{}\n", encoding="utf-8")
    calls = _install_urlopen(monkeypatch, _Response(201, b'{"session_id": "SN1"}'))
    cfg = _cfg(http_port=9090)

    result = report.try_http(cfg, session)
    assert result.ok and result.channel == "http"
    request, timeout = calls[0]
    assert request.full_url == "http://10.0.0.5:9090/sessions"
    assert request.get_method() == "POST"
    assert b'name="file"' in request.data
    assert timeout == cfg.report.http_timeout


def test_try_http_without_endpoint_never_touches_the_network(tmp_path, monkeypatch):
    session = tmp_path / "SN1_20260101T000000Z"
    session.mkdir()
    calls = _install_urlopen(monkeypatch)
    cfg = _cfg()
    cfg.report.ssh_host = ""
    cfg.report.http_base_url = ""

    result = report.try_http(cfg, session)
    assert not result.ok
    assert "no http receiver configured" in result.message
    assert calls == []


# ---------------------------------------------------------------------------
# check_http
# ---------------------------------------------------------------------------
def test_check_http_gets_healthz(monkeypatch):
    calls = _install_urlopen(monkeypatch, _Response(200, b"ok"))
    result = report.check_http(_cfg(http_port=8080), timeout=3.0)
    assert result.ok
    request, timeout = calls[0]
    assert request.full_url == "http://10.0.0.5:8080/healthz"
    assert request.get_method() == "GET"
    assert timeout == 3.0
    assert "reachable" in result.message


def test_check_http_maps_http_error(monkeypatch):
    url = "http://10.0.0.5:8080/healthz"
    error = urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"missing"))
    _install_urlopen(monkeypatch, error=error)
    result = report.check_http(_cfg(http_port=8080))
    assert not result.ok
    assert "HTTP 404" in result.message
    assert "missing" in result.detail


def test_check_http_maps_network_error(monkeypatch):
    _install_urlopen(monkeypatch, error=urllib.error.URLError("connection refused"))
    result = report.check_http(_cfg(http_port=8080))
    assert not result.ok
    assert "http failed" in result.message


def test_check_http_without_endpoint_returns_immediately(monkeypatch):
    calls = _install_urlopen(monkeypatch)
    cfg = _cfg()
    cfg.report.ssh_host = ""
    cfg.report.http_base_url = ""
    result = report.check_http(cfg)
    assert not result.ok
    assert "no http receiver configured" in result.message
    assert calls == []


# ---------------------------------------------------------------------------
# transport ordering inside upload_session
# ---------------------------------------------------------------------------
def _stub_channels(monkeypatch, outcomes):
    calls = []
    statuses = []

    def make(name):
        def run(cfg, session_dir, deadline=None):
            calls.append(name)
            ok = outcomes.get(name, False)
            return report.UploadResult(name, ok, "ok" if ok else "failed")

        return run

    monkeypatch.setattr(report, "try_rsync", make("ssh"))
    monkeypatch.setattr(report, "try_http", make("http"))
    queued = []
    monkeypatch.setattr(
        report, "enqueue_pending", lambda cfg, session: queued.append(session) or Path("/tmp/pending")
    )
    return calls, statuses, queued


def test_upload_session_ssh_first(tmp_path, monkeypatch):
    calls, _statuses, _queued = _stub_channels(monkeypatch, {"ssh": True})
    cfg = _cfg(transport="ssh")
    result = report.upload_session(cfg, tmp_path, skip_route_wait=True)
    assert result.ok and result.channel == "ssh"
    assert calls == ["ssh"]


def test_upload_session_http_first(tmp_path, monkeypatch):
    calls, _statuses, _queued = _stub_channels(monkeypatch, {"http": True})
    cfg = _cfg(transport="http")
    result = report.upload_session(cfg, tmp_path, skip_route_wait=True)
    assert result.ok and result.channel == "http"
    assert calls == ["http"]


def test_upload_session_falls_back_to_other_transport(tmp_path, monkeypatch):
    calls, _statuses, _queued = _stub_channels(monkeypatch, {"http": True})
    cfg = _cfg(transport="ssh")
    result = report.upload_session(cfg, tmp_path, skip_route_wait=True)
    assert result.ok and result.channel == "http"
    assert calls == ["ssh", "http"]


def test_upload_session_queues_when_both_fail(tmp_path, monkeypatch):
    calls, statuses, queued = _stub_channels(monkeypatch, {})
    cfg = _cfg(transport="http")
    result = report.upload_session(
        cfg, tmp_path, skip_route_wait=True, on_status=statuses.append
    )
    assert result.channel == "pending"
    assert calls == ["http", "ssh"]
    assert queued == [tmp_path]
    assert any("transport order" in line for line in statuses)
