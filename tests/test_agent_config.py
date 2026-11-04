from __future__ import annotations

from pathlib import Path

from raidforge.agent.config import AgentConfig


def test_precedence_env_over_run_env_over_yaml(tmp_path: Path):
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        "server_url: https://yaml.example\napi_prefix: /api/yaml\nheartbeat_interval: 20\n"
        "operator:\n  name: yaml-user\n",
        encoding="utf-8",
    )
    run_env = tmp_path / "agent.env"
    run_env.write_text("RF_AGENT_SERVER_URL=https://run.example\nRF_AGENT_HEARTBEAT_INTERVAL=15\n", encoding="utf-8")
    config = AgentConfig.load(
        yaml_path=yaml_path,
        run_env=run_env,
        env={"RF_AGENT_HEARTBEAT_INTERVAL": "12"},
        cmdline=tmp_path / "cmdline",
    )
    assert config.server_url == "https://run.example"
    assert config.heartbeat_interval == 12
    assert config.operator_name == "yaml-user"


def test_api_prefix_normalised(tmp_path: Path):
    config = AgentConfig.load(
        yaml_path=tmp_path / "missing.yaml",
        run_env=tmp_path / "missing.env",
        env={"RF_AGENT_SERVER_URL": "https://host.example", "RF_AGENT_API_PREFIX": "api/raidforge/"},
        cmdline=tmp_path / "cmdline",
    )
    assert config.api_prefix == "/api/raidforge"
    assert config.base_url == "https://host.example/api/raidforge"


def test_http_only_loopback_allowed(tmp_path: Path):
    config = AgentConfig.load(
        yaml_path=tmp_path / "missing.yaml",
        run_env=tmp_path / "missing.env",
        env={"RF_AGENT_SERVER_URL": "http://192.168.1.10:8080"},
        cmdline=tmp_path / "cmdline",
    )
    assert config.server_url == ""
    loopback = AgentConfig.load(
        yaml_path=tmp_path / "missing.yaml",
        run_env=tmp_path / "missing.env",
        env={
            "RF_AGENT_SERVER_URL": "http://127.0.0.1:8080",
            "RF_AGENT_ALLOW_INSECURE_HTTP": "true",
        },
        cmdline=tmp_path / "cmdline",
    )
    assert loopback.server_url == "http://127.0.0.1:8080"


def test_token_from_run_env_and_redacted(tmp_path: Path):
    run_env = tmp_path / "agent.env"
    run_env.write_text("RF_AGENT_TOKEN=secret-token\n", encoding="utf-8")
    config = AgentConfig.load(
        yaml_path=tmp_path / "missing.yaml",
        run_env=run_env,
        env={},
        cmdline=tmp_path / "cmdline",
    )
    assert config.token == "secret-token"
    assert "secret-token" not in str(config.redacted())
    assert config.redacted()["token"] == "***"


def test_cmdline_token(tmp_path: Path):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("boot=live rf.token=rf_live_abc quiet", encoding="utf-8")
    config = AgentConfig.load(
        yaml_path=tmp_path / "missing.yaml",
        run_env=tmp_path / "missing.env",
        env={},
        cmdline=cmdline,
    )
    assert config.token == "rf_live_abc"


def test_verify_tls_forced_true(tmp_path: Path):
    config = AgentConfig.load(
        yaml_path=tmp_path / "missing.yaml",
        run_env=tmp_path / "missing.env",
        env={"RF_AGENT_VERIFY_TLS": "false"},
        cmdline=tmp_path / "cmdline",
    )
    assert config.verify_tls is True
