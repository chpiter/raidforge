from __future__ import annotations

from webplane.config import Settings


def test_default_agent_token_is_raidforge(monkeypatch):
    monkeypatch.delenv("WEBPLANE_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("RAIDFORGE_CP_TOKEN", raising=False)
    settings = Settings.from_env()
    assert settings.agent_token == "raidforge"


def test_explicit_empty_token_disables_auth(monkeypatch):
    monkeypatch.setenv("WEBPLANE_AGENT_TOKEN", "")
    monkeypatch.delenv("RAIDFORGE_CP_TOKEN", raising=False)
    settings = Settings.from_env()
    assert settings.agent_token == ""


def test_custom_token_overrides_default(monkeypatch):
    monkeypatch.setenv("WEBPLANE_AGENT_TOKEN", "secret")
    settings = Settings.from_env()
    assert settings.agent_token == "secret"
