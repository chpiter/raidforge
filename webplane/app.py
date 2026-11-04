"""Flask application factory for the RaidForge control plane."""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, request

from . import __version__
from .api import agent_api, control_api, evidence_api
from .auth import ApiError, error_response
from .config import Settings
from .db import close_db, init_db
from .maintenance import start_maintenance
from .static_site import register_static
from .timeutil import iso

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    init_db(settings)

    app = Flask(__name__, static_folder=None)
    app.config["SETTINGS"] = settings
    app.config["MAX_CONTENT_LENGTH"] = (
        max(settings.max_evidence_bytes, settings.max_session_bytes) + 16 * 1024 * 1024
    )
    app.json.ensure_ascii = False

    app.teardown_appcontext(close_db)
    app.register_blueprint(agent_api.bp)
    app.register_blueprint(control_api.bp)
    app.register_blueprint(evidence_api.bp)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "service": "webplane", "now": iso()})

    @app.after_request
    def _cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Accept, Idempotency-Key, X-Raidforge-Token, "
            "X-Raidforge-Agent-Id, X-Raidforge-Agent-Version, X-Raidforge-Protocol, "
            "X-Raidforge-Lease"
        )
        response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        return response

    @app.before_request
    def _preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            return "", 204
        return None

    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        return error_response(exc)

    @app.errorhandler(404)
    def _not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"error_code": "not_found", "message": "unknown API endpoint"}), 404
        return jsonify({"error_code": "not_found", "message": "not found"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_exc):
        return jsonify({"error_code": "method_not_allowed", "message": "method not allowed"}), 405

    @app.errorhandler(413)
    def _too_large(_exc):
        return jsonify({"error_code": "evidence_too_large", "message": "request too large"}), 413

    @app.errorhandler(500)
    def _server_error(exc):
        log.exception("unhandled error: %s", exc)
        return jsonify({"error_code": "internal_error", "message": "internal server error"}), 500

    register_static(app, settings)

    if settings.maintenance:
        start_maintenance(settings)

    if not settings.agent_token:
        app.logger.warning(
            "WEBPLANE_AGENT_TOKEN 未设置：agent API 不校验令牌；请只在内网/VPN 内使用"
        )
    app.logger.info("webplane %s data=%s", __version__, settings.data_dir)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    app = create_app(settings)
    app.run(
        host=settings.host,
        port=settings.port,
        threaded=True,
        use_reloader=False,
        debug=False,
    )


def __getattr__(name: str):
    """Lazy ``app`` so importing the module in tests does not create data dirs."""

    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)


__all__ = ["create_app", "main"]
