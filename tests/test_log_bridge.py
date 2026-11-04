from __future__ import annotations

import logging

from raidforge.ui import logbus


def _record(name: str, level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, (), None)


def test_heartbeat_info_is_filtered_but_warnings_survive():
    buffer = logbus.RingBuffer()
    handler = logbus.UILogHandler(buffer)

    handler.emit(_record("raidforge.agent.client", logging.INFO, "GET /api/***/agent/tasks/next -> 204"))
    handler.emit(_record("raidforge.agent.client", logging.INFO, "POST /api/***/agent/heartbeat -> 200"))
    assert buffer.drain() == []

    handler.emit(
        _record("raidforge.agent.client", logging.WARNING, "GET /api/***/agent/tasks/next failed")
    )
    lines = buffer.drain()
    assert len(lines) == 1
    assert lines[0][0] == "warn"
    assert "failed" in lines[0][1]


def test_token_is_redacted_and_levels_are_mapped():
    buffer = logbus.RingBuffer()
    handler = logbus.UILogHandler(buffer)
    handler.add_secret("s3cr3t")

    handler.emit(_record("raidforge.agent", logging.INFO, "token=s3cr3t connected"))
    handler.emit(_record("raidforge.agent", logging.ERROR, "boom s3cr3t"))

    lines = buffer.drain()
    assert "s3cr3t" not in "".join(text for _, text in lines)
    assert lines[0][0] == "info"
    assert lines[1][0] == "error"
    assert "***" in lines[0][1]


def test_drain_is_destructive():
    buffer = logbus.RingBuffer()
    buffer.append("info", "one")
    buffer.append("warn", "two")
    assert [text for _, text in buffer.drain()] == ["one", "two"]
    assert buffer.drain() == []
