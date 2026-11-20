from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from f1_strategist.agent.types import LLMStatus
from f1_strategist.config import (
    AgentConfig,
    AppConfig,
    ConfigError,
    LLMConfig,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_llm_and_agent_defaults_keep_the_feature_off() -> None:
    cfg = AppConfig()
    assert not cfg.llm.enabled
    assert cfg.llm.endpoint == "http://127.0.0.1:1234/v1"
    assert cfg.llm.model == ""
    assert cfg.llm.api_key is None
    assert cfg.llm.max_in_flight == 1
    assert cfg.agent.language == "es"
    assert cfg.agent.fallback_on_failure == "template"
    assert cfg.agent.offline_mode == "template"
    assert cfg.agent.timeout_for("critical") == 1.5
    assert cfg.agent.timeout_for("strategic") == 10.0
    assert cfg.agent.timeout_for("informational") == 10.0


def test_llm_rejects_non_http_endpoints() -> None:
    with pytest.raises(ValueError):
        LLMConfig(endpoint="ftp://127.0.0.1:1234/v1")
    with pytest.raises(ValueError):
        LLMConfig(endpoint="127.0.0.1:1234")
    with pytest.raises(ValueError):
        LLMConfig(endpoint="http://user:pass@127.0.0.1:1234/v1")
    with pytest.raises(ValueError):
        LLMConfig(endpoint="http://:1234/v1")
    with pytest.raises(ValueError):
        LLMConfig(endpoint="http://127.0.0.1:notaport/v1")
    assert LLMConfig(endpoint="http://[::1]:1234/v1").endpoint == "http://[::1]:1234/v1"


def test_enabled_llm_requires_a_model() -> None:
    with pytest.raises(ValueError):
        LLMConfig(enabled=True)
    assert LLMConfig(enabled=True, model="local-model").model == "local-model"


def test_invalid_sections_raise_config_error(tmp_path: Path) -> None:
    bad_endpoint = _write(tmp_path, '[llm]\nendpoint = "ws://127.0.0.1:1234"\n')
    with pytest.raises(ConfigError):
        load_config(bad_endpoint)
    bad_timeout = _write(tmp_path, "[agent]\ntimeout_strategic_s = 0\n")
    with pytest.raises(ConfigError):
        load_config(bad_timeout)


def test_api_key_never_appears_in_representations() -> None:
    cfg = AppConfig(llm=LLMConfig(api_key=SecretStr("sk-secret-value")))
    assert "sk-secret-value" not in repr(cfg)
    assert "sk-secret-value" not in repr(cfg.llm)
    assert "sk-secret-value" not in str(cfg.model_dump())
    assert "sk-secret-value" not in cfg.model_dump_json()
    assert cfg.llm.resolved_api_key == "sk-secret-value"


def test_api_key_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("F1_LLM_API_KEY", "sk-from-env")
    cfg = LLMConfig()
    assert cfg.resolved_api_key == "sk-from-env"
    assert cfg.api_key is None


def test_example_config_loads_the_new_sections() -> None:
    cfg = load_config(Path("config/config.example.toml"))
    assert cfg.llm.enabled is False
    assert cfg.agent.max_words == 30
    assert cfg.agent.imperative_phrases


def test_agent_timeout_lookup_covers_every_priority() -> None:
    agent = AgentConfig(
        timeout_critical_s=1.0, timeout_strategic_s=2.0, timeout_informational_s=3.0
    )
    assert agent.timeout_for("critical") == 1.0
    assert agent.timeout_for("strategic") == 2.0
    assert agent.timeout_for("informational") == 3.0
    assert agent.timeout_for("unknown-priority") == 3.0


def test_snapshot_default_status_is_disabled() -> None:
    from f1_strategist.agent.types import AgentSnapshot, LLMHealth

    snapshot = AgentSnapshot()
    assert isinstance(snapshot.llm_status, LLMHealth)
    assert snapshot.llm_status.status is LLMStatus.DISABLED
