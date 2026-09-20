"""Part 4.1 — settings defaults, secret handling and the dev-twin guard."""

import os

import pytest
from pydantic import SecretStr

from config import LLM_COOLDOWN_AUTH, LLM_COOLDOWN_MAX, Settings


def test_defaults_match_spec():
    """The declared defaults, not a constructed Settings: these tests run
    inside tf-ai-agent-dev, whose env deliberately overrides SERVICE_NAME,
    LLM_BASE_URL and LLM_DAILY_CALL_CAP. `_env_file=None` disables the dotenv
    file, not the process environment."""
    declared = {name: f.default for name, f in Settings.model_fields.items()}
    assert declared["service_name"] == "ai-agent"
    assert declared["service_port"] == 8004
    assert declared["llm_base_url"] == "https://openrouter.ai/api/v1"
    assert declared["llm_model"] == "anthropic/claude-sonnet-5"
    assert declared["llm_model_classifier"] == "anthropic/claude-sonnet-5"
    assert declared["llm_daily_call_cap"] == 100
    assert declared["llm_max_tokens"] == 8000
    assert declared["llm_timeout"] == 150.0
    assert declared["llm_cooldown_seconds"] == 900
    assert declared["llm_referer"] == ""
    assert declared["llm_title"] == ""
    assert declared["llm_api_key"].get_secret_value() == ""
    assert LLM_COOLDOWN_MAX == 3600
    assert LLM_COOLDOWN_AUTH == 3600


def test_key_reads_openrouter_env_var(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    s = Settings(_env_file=None)
    assert s.llm_configured is True
    assert s.llm_api_key.get_secret_value() == "sk-test"


def test_key_is_masked_in_repr_and_str():
    s = Settings(_env_file=None, llm_api_key=SecretStr("sk-supersecret"))
    assert "sk-supersecret" not in repr(s)
    assert "sk-supersecret" not in str(s)
    assert "sk-supersecret" not in repr(s.llm_api_key)


def test_empty_key_is_not_configured():
    assert Settings(_env_file=None, llm_api_key=SecretStr("")).llm_configured is False


def test_asyncpg_url_strips_driver():
    s = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@h:5432/db")
    assert s.asyncpg_url == "postgresql://u:p@h:5432/db"


def test_unknown_env_vars_are_ignored(monkeypatch):
    """LLM_PROVIDER / ANTHROPIC_API_KEY / GOOGLE_AI_API_KEY are still set on
    the prod compose block. One provider in code means they are ignored, not
    an error — that is what lets the prod block stay untouched."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-ignored")
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "ignored-too")
    s = Settings(_env_file=None)
    assert not hasattr(s, "llm_provider")
    assert s.llm_configured is False


@pytest.mark.skipif(
    os.getenv("SERVICE_NAME") != "ai-agent-dev",
    reason="twin guard; only meaningful inside tf-ai-agent-dev",
)
def test_twin_never_calls_openrouter():
    """Five locks, all hard-coded in docker-compose.yml's ai-agent-dev block.
    Never drop any of them (CLAUDE.md)."""
    s = Settings()
    assert s.llm_api_key.get_secret_value() == ""
    assert s.llm_configured is False
    assert s.llm_daily_call_cap == 0
    assert ".invalid" in s.llm_base_url
    assert s.database_url.endswith("/tradingfirm_dev")
    assert s.redis_url.endswith("/1")
