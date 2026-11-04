"""Bridge Python logging into the TUI message panel.

The TUI owns the terminal while ``app.run()`` is active.  A normal
``StreamHandler`` keeps a reference to the real stderr and paints log lines
straight onto the alternate screen, which is what used to cover the NavBar.
This module collects those records in a thread-safe buffer instead; the
message panel drains it on a timer.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Deque, List, Optional, Set, Tuple

LogLine = Tuple[str, str]  # (level, text)

_KEEP_LINES = 400


class RingBuffer:
    """Thread-safe hand-off between logging threads and the UI thread."""

    def __init__(self, maxlen: int = _KEEP_LINES) -> None:
        self._lines: Deque[LogLine] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, level: str, text: str) -> None:
        with self._lock:
            self._lines.append((level, text))

    def drain(self) -> List[LogLine]:
        with self._lock:
            items = list(self._lines)
            self._lines.clear()
            return items

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class HeartbeatFilter(logging.Filter):
    """Drop the 3s poll / 10s heartbeat INFO lines, keep warnings and errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if record.name == "raidforge.agent.client":
            message = record.getMessage()
            if "/agent/tasks/next" in message or "/agent/heartbeat" in message:
                return False
        return True


class UILogHandler(logging.Handler):
    """Format records into the ring buffer (never writes to the terminal)."""

    def __init__(self, buffer: RingBuffer, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.buffer = buffer
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
        )
        self.addFilter(HeartbeatFilter())
        self._secrets: Set[str] = set()
        self._secret_lock = threading.Lock()

    def add_secret(self, secret: str) -> None:
        secret = str(secret or "")
        if not secret:
            return
        with self._secret_lock:
            self._secrets.add(secret)

    def clear_secrets(self) -> None:
        with self._secret_lock:
            self._secrets.clear()

    def redact(self, text: str) -> str:
        with self._secret_lock:
            secrets = tuple(self._secrets)
        for secret in secrets:
            text = text.replace(secret, "***")
        return text

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self.filter(record):
                return
            level = "error" if record.levelno >= logging.ERROR else (
                "warn" if record.levelno >= logging.WARNING else "info"
            )
            text = self.redact(self.format(record))
            for line in text.splitlines() or [text]:
                self.buffer.append(level, line)
        except Exception:  # pragma: no cover - logging must never break the UI
            self.handleError(record)


class _RedactingFormatter(logging.Formatter):
    def __init__(self, handler: UILogHandler, fmt: str, datefmt: str) -> None:
        super().__init__(fmt, datefmt=datefmt)
        self._ui_handler = handler

    def format(self, record: logging.LogRecord) -> str:
        return self._ui_handler.redact(super().format(record))


_buffer = RingBuffer()
_handler = UILogHandler(_buffer)
_state_lock = threading.Lock()
_installed = False
_original_handlers: List[logging.Handler] = []
_file_handler: Optional[logging.FileHandler] = None


def buffer() -> RingBuffer:
    return _buffer


def active() -> Optional[UILogHandler]:
    """Return the installed handler, or ``None`` outside the TUI."""
    return _handler if _installed else None


def add_secret(secret: str) -> None:
    _handler.add_secret(secret)


def reset() -> None:
    """Test helper: clear buffered lines and secrets."""
    _buffer.clear()
    _handler.clear_secrets()


def install(log_path: Optional[Path] = None) -> UILogHandler:
    """Replace stderr logging with the UI bridge for the lifetime of the TUI."""

    global _installed, _original_handlers, _file_handler
    with _state_lock:
        if _installed:
            return _handler
        root = logging.getLogger()
        _original_handlers = list(root.handlers)
        real_streams = {sys.stderr, getattr(sys, "__stderr__", sys.stderr)}
        for handler in list(root.handlers):
            if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) in real_streams:
                root.removeHandler(handler)
        root.addHandler(_handler)
        if log_path is not None:
            try:
                path = Path(log_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(
                    str(path), maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
                )
                handler.setFormatter(
                    _RedactingFormatter(
                        _handler,
                        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        "%Y-%m-%d %H:%M:%S",
                    )
                )
                root.addHandler(handler)
                _file_handler = handler
            except OSError:
                _file_handler = None
        _installed = True
        return _handler


def restore() -> None:
    """Put the pre-TUI handlers back so post-exit logging still reaches stderr."""

    global _installed, _original_handlers, _file_handler
    with _state_lock:
        if not _installed:
            return
        root = logging.getLogger()
        root.removeHandler(_handler)
        if _file_handler is not None:
            root.removeHandler(_file_handler)
            try:
                _file_handler.close()
            except Exception:
                pass
            _file_handler = None
        for handler in _original_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        _original_handlers = []
        _installed = False


__all__ = [
    "HeartbeatFilter",
    "RingBuffer",
    "UILogHandler",
    "active",
    "add_secret",
    "buffer",
    "install",
    "reset",
    "restore",
]
