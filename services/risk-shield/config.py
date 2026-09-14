"""
TradingFirm — Risk Shield Configuration

Loads settings from environment variables with sensible defaults.
Mirrors services/data-engine/config.py in shape; the two services share no
Python package, so this is a copied pattern, not imported code (Part 3.1).
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings

# Hard bound on any single dependency connection attempt at startup, in
# seconds (Part 3.1 decision 6). Fail-open is only true if the attempt
# *returns*: asyncpg's default connect timeout is 60 s and redis-py's is
# unbounded, so a Postgres that is restarting rather than refusing would
# stall this service's boot with /health unreachable.
#
# Read it as `config.STARTUP_TIMEOUT` at call time, never
# `from config import STARTUP_TIMEOUT` — a from-import copies the value and
# the lifespan tests monkeypatch this down to keep the slow paths fast.
STARTUP_TIMEOUT = 5.0

# How long shutdown waits for the cancelled scheduler task before closing
# the pool and Redis anyway (Part 3.4 decision 8). A check inside the
# yfinance thread cannot be cancelled. Read at call time, like the above.
SCHEDULER_SHUTDOWN_TIMEOUT = 5.0


class Settings(BaseSettings):
    """Risk Shield service configuration."""

    # Service identity
    service_name: str = "risk-shield"
    service_port: int = 8003

    # Database (asyncpg)
    database_url: str = "postgresql+asyncpg://tf_user:tradingfirm_dev_2026@postgres:5432/tradingfirm"

    # Redis
    redis_url: str = "redis://redis:6379"

    # FRED (Part 3.2 macro series). SecretStr so repr()/str() mask it by
    # construction — a leak needs a deliberate .get_secret_value(), not a
    # forgotten f-string (G14). Empty = the fetcher raises before any HTTP;
    # the dev twin is always empty. Nothing in 3.1 reads it beyond the
    # `fredConfigured` boolean on /health.
    fred_api_key: SecretStr = SecretStr("")

    # Finnhub (Part 3.5 market news). SecretStr like FRED's. It goes in the
    # X-Finnhub-Token header only, never a URL. Empty = the client raises
    # before any HTTP; the dev twin is always empty.
    finnhub_api_key: SecretStr = SecretStr("")

    # Market news poller (Part 3.5). Off unless the environment turns it on:
    # only the prod compose service does. The dev twin hard-codes false.
    news_poll_enabled: bool = False

    # Where the poller POSTs /news/ingest. The dev twin hard-codes
    # http://data-engine-dev:8001 so it can never write into prod's database.
    data_engine_url: str = "http://data-engine:8001"

    # Regime scheduler (Part 3.4). Off unless the environment turns it on:
    # only the prod compose service does. The dev twin has no quotes fixture,
    # so a scheduler there would download from yfinance every 5 minutes.
    scheduler_enabled: bool = False

    # Pub/sub channel for health changes. Redis pub/sub ignores the DB index,
    # so the dev twin (Redis DB 1) overrides this to stay off prod's channel.
    # Default = shared/constants.py REDIS_CHANNELS["health_update"].
    health_channel: str = "tf:risk:health"

    # Macro brief (Part 3.6a plumbing; the generator is 3.6b). Off unless the
    # environment turns it on, and prod keeps it off until ai-agent's
    # POST /brief/macro (Phase 4.6) exists. The dev twin hard-codes false.
    macro_brief_enabled: bool = False

    # Where 3.6b calls ai-agent. The dev twin hard-codes an .invalid host
    # (RFC 6761: it never resolves), so the twin can never reach an LLM.
    ai_agent_url: str = "http://ai-agent:8004"

    # Weekend-exposure situation route (Part 3.4c decision 2). The shared
    # secret for the service's first *write* endpoint, sent as X-TF-Token
    # and compared with hmac.compare_digest. SecretStr like the API keys, so
    # a repr() cannot leak it (G14). **Empty = the route answers 503, never
    # falls open**; the dev twin is always empty.
    weekend_write_token: SecretStr = SecretStr("")

    # Debug mode
    debug: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def asyncpg_url(self) -> str:
        """Strip +asyncpg from SQLAlchemy-style URL for raw asyncpg."""
        return self.database_url.replace("+asyncpg", "")

    @property
    def fred_configured(self) -> bool:
        """Whether a FRED key is present. The only thing 3.1 asks of the
        key — the value itself never reaches a response or a log."""
        return bool(self.fred_api_key.get_secret_value())

    @property
    def finnhub_configured(self) -> bool:
        """Whether a Finnhub key is present, never the value (G14)."""
        return bool(self.finnhub_api_key.get_secret_value())

    @property
    def weekend_write_configured(self) -> bool:
        """Whether the situation route has a secret, never the value (G14).
        False = the route is disabled, which is the twin's state."""
        return bool(self.weekend_write_token.get_secret_value())


settings = Settings()
