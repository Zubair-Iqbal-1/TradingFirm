"""
TradingFirm — AI Agent Configuration (Part 4.1)

Loads settings from environment variables with sensible defaults. Mirrors
services/risk-shield/config.py in shape; the two services share no Python
package, so this is a copied pattern, not imported code.

One provider in code (decisions.md 2026-09-20, supersedes D16): an
OpenAI-compatible client with a configurable base_url, pointed at
OpenRouter. Models are env knobs, so Anthropic and Z.ai ids are reachable
through the same key.
"""

import re

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings

# Hard bound on any single dependency connection attempt at startup, in
# seconds — risk-shield's STARTUP_TIMEOUT, same reasoning (redis-py's own
# socket timeout is unbounded). 4.1 builds no lifespan; cache.create_redis
# reads this at call time, never via a from-import, so a test can move it.
STARTUP_TIMEOUT = 5.0

# Ceiling on any cooldown this service will honour, in seconds. A hostile or
# confused `retry-after` cannot park the analyst for a day.
LLM_COOLDOWN_MAX = 3600

# Cooldown after 401 / 402 / 403. An hour, because none of the three clears
# on its own: the key is wrong, or the account is out of credits.
LLM_COOLDOWN_AUTH = 3600

# One OpenRouter provider slug ("anthropic", "google-vertex",
# "amazon-bedrock/us"): a charset that cannot carry anything but a name.
PROVIDER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,63}$")


def parse_provider_order(value) -> list[str]:
    """LLM_PROVIDER_ORDER as a list, in order. ValueError when it is empty or
    holds anything that is not a slug: the provider object rides on every
    request, so there is no "unset" to fall back to."""
    if not isinstance(value, str):
        raise ValueError("LLM_PROVIDER_ORDER must be a string")
    slugs = [part.strip().lower() for part in value.split(",")]
    if not value.strip() or any(not PROVIDER_SLUG_RE.match(slug) for slug in slugs):
        raise ValueError(
            "LLM_PROVIDER_ORDER must be one or more OpenRouter provider slugs, "
            "comma-separated (e.g. anthropic); it cannot be empty"
        )
    return slugs


class Settings(BaseSettings):
    """AI Agent service configuration."""

    # Service identity
    service_name: str = "ai-agent"
    service_port: int = 8004

    # Database (asyncpg). Still unopened: 4.2 writes no ai.* table — the
    # classifier's durable record is data-engine's news_items.sentiment,
    # reached over HTTP (spec 4.2 decision 1). The pool arrives with
    # migration 005_ai.sql in Part 4.4.
    database_url: str = "postgresql+asyncpg://tf_user:tradingfirm_dev_2026@postgres:5432/tradingfirm"

    # Redis
    redis_url: str = "redis://redis:6379"

    # The one knob that moves this service to another OpenAI-compatible
    # gateway. The dev twin points it at an .invalid host (RFC 6761).
    llm_base_url: str = "https://openrouter.ai/api/v1"

    # OpenRouter key. SecretStr like risk-shield's FRED and Finnhub keys, so
    # repr()/str() mask it by construction and a leak needs a deliberate
    # .get_secret_value() rather than a forgotten f-string (G14). Empty = the
    # provider raises before any HTTP and before a client is built. The dev
    # twin is always empty.
    # AliasChoices, not a bare alias: pydantic-settings resolves the init
    # source through the alias too, so a bare validation_alias would make
    # Settings(llm_api_key=...) silently do nothing — which is how every test
    # builds a configured settings object.
    llm_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "llm_api_key"),
    )

    # Verdicts, judgments, macro brief. Sonnet 5 for now; the GLM ids in
    # providers.openai_compat_provider.MODEL_REASONING are the env-switch
    # options for the 4.8 comparison.
    llm_model: str = "anthropic/claude-sonnet-5"

    # Headline classification (4.2). Same default, its own knob.
    llm_model_classifier: str = "anthropic/claude-sonnet-5"

    # Hard bound on calls *made* per ET day. 0 = every call refused, which is
    # how the dev twin is nailed shut without relying on the empty key alone.
    llm_daily_call_cap: int = 100

    # The classifier's own, smaller cap (Part 4.2). Separate so a batch loop
    # cannot eat the analyst's budget: ~400 unique headlines a day at 30 per
    # batch is ~14 calls, so 40 is about 3x headroom and still leaves 60 of
    # llm_daily_call_cap for verdicts, judgments and the macro brief. The dev
    # twin hard-codes it to 0.
    llm_classifier_daily_call_cap: int = 40

    # Non-streaming output budget. At reasoning effort "low" roughly a fifth
    # goes to reasoning and the rest to the JSON answer.
    llm_max_tokens: int = 8000

    # Seconds, hard, per call. risk-shield's BRIEF_TIMEOUT is 180 s, so
    # ai-agent must give up first or its caller times out on a live call.
    llm_timeout: float = 150.0

    # Cooldown after a 429 with no usable retry-after. Matches every other
    # source cooldown in the repo.
    llm_cooldown_seconds: int = 900

    # OpenRouter provider routing (Part 4.4). Every request carries
    # provider: {"order": [...], "allow_fallbacks": true}: try the listed
    # hosts first, in this order (comma-separated), and let OpenRouter fall
    # back to another host if they cannot serve — a preference, never a pin,
    # so a host outage degrades the prompt cache and not the analyst. It
    # exists because a prompt cache is per host, and unordered routing across
    # five hosts rarely reads what it wrote. The host that actually served
    # each call is stored in ai.llm_calls.host, and a fallback is a WARNING.
    # Empty or malformed is a config error and the service refuses to start:
    # there is no way to switch the provider object off.
    llm_provider_order: str = "anthropic"

    @field_validator("llm_provider_order")
    @classmethod
    def _provider_order_is_slugs(cls, value: str) -> str:
        return ",".join(parse_provider_order(value))

    # OpenRouter app attribution. Sent as HTTP-Referer / X-Title only when
    # non-empty; never a hard-coded value.
    llm_referer: str = ""
    llm_title: str = ""

    # data-engine, for the classifier's write-back (Part 4.2). The dev twin
    # hard-codes the dev twin's own host, so nothing on 8014 can ever write
    # into prod's news_items — test_twin_never_writes_prod_data_engine.
    data_engine_url: str = "http://data-engine:8001"

    # Seconds, hard, per write-back call. Write-back is fail-open: a timeout
    # is counted and logged, never raised at the caller.
    data_engine_timeout: float = 10.0

    # Part 4.4 — /analyze.
    # risk-shield, for the regime and the macro brief. Fail-open, so short.
    # The dev twin hard-codes risk-shield-dev (test_twin_never_calls_prod_risk_shield).
    risk_shield_url: str = "http://risk-shield:8003"
    risk_shield_timeout: float = 5.0

    # A cold dossier makes ~10 upstream calls and took 9.7 s in 2.5's live
    # check; its slowest source is bounded at 8 s. Fail-closed.
    dossier_timeout: float = 60.0

    # The per-ticker verdict cache (D18). Flat: ai-agent has no exchange
    # calendar. The input fingerprint, not the clock, does the real work.
    verdict_cache_ttl: int = 14400

    # cache_control on the verdict's system block. Off until a live call
    # shows the prefix clears Sonnet 5's 1,024-token minimum (cacheWrite > 0).
    llm_verdict_cache: bool = False

    # The journal scorer's nightly slot (Part 4.5): 17:30 ET on XNYS sessions,
    # refreshing due tickers through data-engine. Off by default; the prod
    # compose block turns it on and the dev twin hard-codes it off
    # (test_twin_never_scores_on_a_schedule).
    journal_scoring_enabled: bool = False

    # Debug mode
    debug: bool = False

    # LLM_PROVIDER / ANTHROPIC_API_KEY / GOOGLE_AI_API_KEY were dropped from
    # the prod compose block in Part 4.2. extra="ignore" stays: an env that
    # still carries them (an old .env, a stale container) must not fail boot.
    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def asyncpg_url(self) -> str:
        """Strip +asyncpg from SQLAlchemy-style URL for raw asyncpg."""
        return self.database_url.replace("+asyncpg", "")

    @property
    def llm_configured(self) -> bool:
        """Whether an OpenRouter key is present. The only thing anything asks
        of the key — the value itself never reaches a response or a log."""
        return bool(self.llm_api_key.get_secret_value())


settings = Settings()
