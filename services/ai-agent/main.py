"""
TradingFirm — AI Agent (Service 5)

Responsibilities:
  - Headline classification (Part 4.2)
  - Verdicts, judgments and the macro brief (Parts 4.3–4.7)
  - One LLM provider: an OpenAI-compatible client pointed at OpenRouter

Endpoints:
  GET  /health              — service health: dependency state at boot, caps
  GET  /                    — service info
  GET  /usage               — today's and this month's LLM spend and call
                              counts, readable without logging into OpenRouter
  POST /classify/headlines  — classify up to 30 headlines in one call and
                              write each result back to data-engine (Part 4.2)

Port: 8004. The prod service publishes it on 127.0.0.1 only (spec 4.2
decision 17): /classify/headlines spends money and /usage reports spend, so
neither is reachable from the LAN. Other services reach this one by compose
DNS, which does not use the published port.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

import cache
import classifier
import config
import prompts
from config import settings
from providers.base import (
    LLMBadResponse,
    LLMCapExceeded,
    LLMCooledDown,
    LLMNotConfigured,
    LLMRateLimited,
    LLMRefused,
    LLMRejected,
    LLMUnavailable,
)

SERVICE_NAME = os.getenv("SERVICE_NAME", "ai-agent")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8004"))

# ── Logging ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ai-agent")


# ── Lifespan (startup/shutdown) ──────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Open Redis and one HTTP client on startup, close them on shutdown.

    Fail-open and bounded, risk-shield's shape (Part 3.1 decision 6): the
    dependency gets config.STARTUP_TIMEOUT seconds, a failure or a timeout
    logs a warning and leaves it None, and the service still boots and serves
    /health. A classifier call with no Redis still works — the cap and the
    cooldown fall back in-process and nothing is cached.

    config.STARTUP_TIMEOUT is read here at call time, never bound at import,
    so a test can move it without the module having copied the old value.

    No database pool: 4.2 writes no ai.* table (spec decision 1). The pool
    arrives with migration 005_ai.sql in Part 4.4.
    """
    logger.info("Starting AI Agent...")

    # Redis (optional). The factory and its PING are bounded together: a
    # Redis that accepts the socket and then never answers is as bad as one
    # that never accepts it.
    try:
        app.state.redis = await asyncio.wait_for(
            cache.create_redis(), timeout=config.STARTUP_TIMEOUT
        )
        logger.info("✅ Redis connection ready")
    except Exception as e:
        logger.warning(f"⚠️  Redis unavailable (caps and cache in-process only): {e!r}")
        app.state.redis = None

    # In-process fallbacks. One MemoryCap per counter, never one shared
    # instance: the provider owns the global cap's, the classifier owns its
    # own (cache.MemoryCap's docstring says why).
    app.state.memory_caps = {
        cache.STATE_LLM_CALLS: cache.MemoryCap(),
        cache.STATE_CLASSIFIER_CALLS: cache.MemoryCap(),
    }
    app.state.memory_cost = cache.MemoryCost()

    # One httpx client for data-engine write-backs, reused across requests.
    import httpx
    app.state.http = httpx.AsyncClient(
        timeout=settings.data_engine_timeout, follow_redirects=False
    )

    # One provider for the process. It takes the Redis client as a
    # constructor argument, so it must be built after Redis is resolved —
    # and it holds no socket of its own until the first call (the
    # AsyncOpenAI client is built lazily, which is what keeps "no key -> no
    # client" literally true).
    from providers import build_provider
    app.state.provider = build_provider(settings, app.state.redis)
    logger.info(
        f"Provider ready (configured={settings.llm_configured}, "
        f"model={settings.llm_model}, classifier={settings.llm_model_classifier})"
    )

    yield

    logger.info("Shutting down AI Agent...")
    if app.state.http is not None:
        await app.state.http.aclose()
    if app.state.redis is not None:
        try:
            await app.state.redis.aclose()
        except Exception as e:
            logger.warning(f"Redis close failed: {e!r}")
    app.state.provider = None


app = FastAPI(
    title="TradingFirm — AI Agent",
    description="LLM-powered headline classification, verdicts and reporting",
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check endpoint.

    `llmConfigured` is a bool and nothing else: whether a key is present is
    the only thing anything may ask about it (G14).
    """
    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.2.0",
        "redis": getattr(app.state, "redis", None) is not None,
        "llmConfigured": settings.llm_configured,
        "caps": {
            "llmDaily": settings.llm_daily_call_cap,
            "classifierDaily": settings.llm_classifier_daily_call_cap,
        },
    }


@app.get("/")
async def root():
    """Root endpoint with service info."""
    return {
        "service": SERVICE_NAME,
        "description": "LLM-powered headline classification, verdicts and reporting",
        "docs": "/docs",
        "health": "/health",
        "endpoints": [
            "GET  /usage",
            "POST /classify/headlines",
        ],
    }


@app.get("/usage")
async def usage():
    """
    Today's and this month's LLM spend and call counts (Part 4.2).

    Redis-read only: no LLM call, no database. `source` is "redis" or
    "memory" — a "memory" answer is this process's own counting since its
    last restart, not the account's, so a small number there means Redis is
    down rather than that little was spent.

    The figures are a floor, not an audit: Redis persistence is weak until
    the housekeeping batch gives it a declared volume and AOF (spec 4.2
    decision 19).
    """
    numbers = await cache.read_usage(
        getattr(app.state, "redis", None),
        getattr(app.state, "memory_caps", None) or {
            cache.STATE_LLM_CALLS: cache.MemoryCap(),
            cache.STATE_CLASSIFIER_CALLS: cache.MemoryCap(),
        },
        getattr(app.state, "memory_cost", None) or cache.MemoryCost(),
    )
    # The provider owns the in-process cooldown clock, so ask it rather than
    # a fresh one: with Redis down, its clock is the only record that the
    # gateway refused us.
    provider = getattr(app.state, "provider", None)
    seconds_left, cause = await cache.cooldown_remaining(
        getattr(app.state, "redis", None),
        getattr(provider, "_memory_cooldowns", None),
        cache.SOURCE_LLM,
        settings.llm_cooldown_seconds,
    )
    return {
        "day": numbers["day"],
        "month": numbers["month"],
        "source": numbers["source"],
        "calls": {
            "llmToday": numbers["llmCalls"],
            "classifierToday": numbers["classifierCalls"],
            "costMissingToday": numbers["costMissing"],
        },
        "caps": {
            "llmDaily": settings.llm_daily_call_cap,
            "classifierDaily": settings.llm_classifier_daily_call_cap,
        },
        "costUsd": {
            "today": numbers["costToday"],
            "month": numbers["costMonth"],
        },
        "cooldown": {
            "source": cache.SOURCE_LLM,
            "secondsLeft": seconds_left,
            "cause": cause,
        },
    }


# ── Headline classification (Part 4.2) ───────────────────────────


class HeadlineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=classifier.TITLE_MAX)
    id: Optional[int] = None
    url: Optional[str] = Field(default=None, max_length=2048)
    source: Optional[str] = Field(default=None, max_length=100)
    publishedAt: Optional[AwareDatetime] = None
    summary: Optional[str] = Field(default=None, max_length=10000)
    ticker: Optional[str] = Field(default=None, max_length=10)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title is blank")
        if "\x00" in value:
            raise ValueError("NUL character not allowed")
        return value


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[HeadlineItem] = Field(min_length=1, max_length=classifier.BATCH_MAX)
    writeBack: bool = True
    # Part 4.4: slugs already given to this ticker's stories, offered to the
    # model for reuse. Each must be a valid slug — a free-text list here
    # would be a way to put arbitrary text into the prompt.
    knownEventKeys: list[str] = Field(default_factory=list, max_length=classifier.KNOWN_KEYS_MAX)

    @field_validator("knownEventKeys")
    @classmethod
    def _known_keys_are_slugs(cls, value: list[str]) -> list[str]:
        for key in value:
            if not classifier.valid_event_key(key):
                raise ValueError("knownEventKeys entries must be 2-8 word lowercase slugs")
        return value


@app.post("/classify/headlines")
async def classify_headlines(body: ClassifyRequest):
    """
    Classify up to 30 headlines in one LLM call (Part 4.2).

    Anything already classified inside the 7-day digest window is served from
    Redis and never sent. Every item carrying an `id` is written back to
    data-engine — cached or freshly classified (spec decision 6b), because
    that is what stops a fail-open write-back from leaving a row NULL
    forever.

    429 — either cap, the cooldown, or this call's own 429.
    502 — the model answered, but the answer is unusable.
    503 — no key, or the gateway could not be reached.
    500 — our prompt, schema or label is wrong. Never the caller's fault:
          a bad body is a 422 from the model above, before anything is
          reserved.
    """
    headlines = [item.model_dump() for item in body.items]
    for headline in headlines:
        if headline.get("publishedAt") is not None:
            headline["publishedAt"] = headline["publishedAt"].isoformat()

    provider = getattr(app.state, "provider", None)
    if provider is None:
        raise HTTPException(status_code=503, detail="provider unavailable")

    redis = getattr(app.state, "redis", None)
    caps = getattr(app.state, "memory_caps", None) or {}
    memory_cap = caps.get(cache.STATE_CLASSIFIER_CALLS) or cache.MemoryCap()
    memory_cost = getattr(app.state, "memory_cost", None) or cache.MemoryCost()

    try:
        results, result = await classifier.classify(
            provider, redis, memory_cap, memory_cost, headlines,
            model=settings.llm_model_classifier,
            cap=settings.llm_classifier_daily_call_cap,
            known_event_keys=body.knownEventKeys,
        )
    except (LLMCapExceeded, LLMCooledDown, LLMRateLimited) as e:
        logger.warning(f"/classify/headlines refused: {type(e).__name__}")
        raise HTTPException(status_code=429, detail=str(e)) from None
    except LLMNotConfigured as e:
        # LLMAuthFailed is a subclass: a bad key and no key fail the same way.
        logger.warning(f"/classify/headlines: {type(e).__name__}")
        raise HTTPException(status_code=503, detail=str(e)) from None
    except LLMUnavailable as e:
        logger.warning(f"/classify/headlines: gateway unavailable ({e})")
        raise HTTPException(status_code=503, detail=str(e)) from None
    except (LLMRejected, LLMRefused, LLMBadResponse, classifier.BatchRejected) as e:
        logger.error(f"/classify/headlines: unusable answer ({type(e).__name__}: {e})")
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from None
    except (prompts.PromptMissing, ValueError) as e:
        # Our own request is wrong — the classifier builds it, not the caller.
        logger.error(f"/classify/headlines: bad request built by this service ({e})")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from None

    written, errors = 0, 0
    if body.writeBack:
        written, errors = await classifier.write_back(
            getattr(app.state, "http", None), settings.data_engine_url,
            [item.id for item in body.items], results,
        )

    cached_count = sum(1 for c in results if c["cached"])
    return {
        "count": len(results),
        "cached": cached_count,
        "classified": len(results) - cached_count,
        "writtenBack": written,
        "writeBackErrors": errors,
        "model": result.model if result is not None else None,
        "usage": result.usage if result is not None else {},
        "items": [
            {
                "id": item.id,
                "digest": cache.headline_digest(item.title, item.url),
                "relevance": c["relevance"],
                "sentiment": c["sentiment"],
                "category": c["category"],
                "oneLine": c["oneLine"],
                "eventKey": c["eventKey"],
                "cached": c["cached"],
            }
            for item, c in zip(body.items, results)
        ],
    }
