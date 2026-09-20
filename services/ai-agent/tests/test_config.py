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
    """Six locks, all hard-coded in docker-compose.yml's ai-agent-dev block.
    Never drop any of them (CLAUDE.md). The classifier cap joined in 4.2:
    it refuses before the global cap does, so a batch cannot reach the wire
    even if the other locks were loosened one at a time."""
    s = Settings()
    assert s.llm_api_key.get_secret_value() == ""
    assert s.llm_configured is False
    assert s.llm_daily_call_cap == 0
    assert s.llm_classifier_daily_call_cap == 0
    assert ".invalid" in s.llm_base_url
    assert s.database_url.endswith("/tradingfirm_dev")
    assert s.redis_url.endswith("/1")


def test_twin_never_writes_prod_data_engine():
    """The seventh lock (Part 4.2). The classifier writes back over HTTP, so
    without this the twin's tests could store classifications in prod's
    news_items. Hard-coded in the ai-agent-dev block; never drop it."""
    s = Settings()
    assert s.data_engine_url == "http://data-engine-dev:8001"
    assert "//data-engine:" not in s.data_engine_url


def test_classifier_cap_default_is_smaller_than_the_global_one():
    """Declared defaults, read from the model rather than from this env —
    the twin hard-codes both caps to 0. Spec 4.2 gate ii: ~400 unique
    headlines a day at 30 per batch is ~14 calls, so 40 is ~3x headroom and
    leaves 60 of the global 100 for verdicts and the macro brief."""
    global_cap = Settings.model_fields["llm_daily_call_cap"].default
    classifier_cap = Settings.model_fields["llm_classifier_daily_call_cap"].default
    assert (global_cap, classifier_cap) == (100, 40)
    assert classifier_cap < global_cap
    assert global_cap - classifier_cap == 60


def test_data_engine_defaults_are_the_prod_service():
    assert Settings.model_fields["data_engine_url"].default == "http://data-engine:8001"
    assert Settings.model_fields["data_engine_timeout"].default == 10.0
