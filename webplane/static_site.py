"""Serve the built Vue application with SPA fallback."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, send_from_directory


def register_static(app: Flask, settings) -> None:
    dist = Path(settings.static_dir)

    def _index():
        target = dist / "index.html"
        if target.is_file():
            return send_from_directory(dist, "index.html")
        return (
            jsonify(
                {
                    "error_code": "frontend_not_built",
                    "message": "webplane/web/dist 不存在，请先运行 npm run build",
                }
            ),
            503,
        )

    @app.get("/")
    def index():
        return _index()

    @app.get("/<path:path>")
    def spa(path: str):
        if path.startswith("api/") or path == "api":
            return jsonify({"error_code": "not_found", "message": "unknown API endpoint"}), 404
        candidate = (dist / path).resolve()
        root = dist.resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            response = send_from_directory(dist, path)
            if path.startswith("assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response
        return _index()


__all__ = ["register_static"]
