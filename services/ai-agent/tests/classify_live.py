"""
LIVE OpenRouter check for the headline classifier (Part 4.2, spec decision 14).

*** THIS SCRIPT SPENDS REAL MONEY. IT IS NOT A TEST. ***

It is not collected by pytest (pytest.ini restricts discovery to test_*.py) and
it must never run in CI. Run it by hand, once, after:

  1. you have put a single line OPENROUTER_API_KEY=<key> in .env yourself, and
  2. you have said "go" in chat.

It refuses to run unless the key is present and the cap allows a call, and it
does one headline first, then five — never the other order (G2).

Expected cost at Sonnet 5 list ($2.00 / $10.00 per MTok): about $0.006 for the
single headline and about $0.01 for the five, so under $0.02 for a full run.
The real figure is printed from OpenRouter's own `usage.cost`.

Run it in the PROD container, which is the only place with a key:

    docker exec tf-ai-agent python3 tests/classify_live.py

What it proves that no mock can: that `strict: true` structured output
actually holds on the wire for anthropic/claude-sonnet-5. That was the one
open risk 4.1 accepted (OpenRouter: "exact compliance is not guaranteed on
every endpoint"), and the classifier's own item contract is the mitigation.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache          # noqa: E402
import classifier     # noqa: E402
from config import settings   # noqa: E402
from providers import build_provider   # noqa: E402

ONE = [{"title": "Nvidia cuts Q4 revenue guidance on weaker data-center demand",
        "url": "https://example.invalid/live-check/1",
        "source": "Reuters", "publishedAt": None, "summary": None, "ticker": "NVDA"}]

FIVE = [
    {"title": "Fed holds rates steady, signals one cut in 2027",
     "url": "https://example.invalid/live-check/2", "source": "AP",
     "publishedAt": None, "summary": None, "ticker": None},
    {"title": "Morgan Stanley upgrades Ford to overweight, target $18",
     "url": "https://example.invalid/live-check/3", "source": "CNBC",
     "publishedAt": None, "summary": None, "ticker": "F"},
    {"title": "Is Palantir stock a buy right now?",
     "url": "https://example.invalid/live-check/4", "source": "Motley Fool",
     "publishedAt": None, "summary": None, "ticker": "PLTR"},
    {"title": "FDA places clinical hold on Sarepta gene therapy trial",
     "url": "https://example.invalid/live-check/5", "source": "Endpoints",
     "publishedAt": None, "summary": None, "ticker": "SRPT"},
    {"title": "Apple CFO sells 45,000 shares under 10b5-1 plan",
     "url": "https://example.invalid/live-check/6", "source": "Barron's",
     "publishedAt": None, "summary": None, "ticker": "AAPL"},
]


def refuse(reason: str) -> None:
    print(f"REFUSING to run the live check: {reason}", file=sys.stderr)
    sys.exit(2)


def preflight() -> None:
    """Checked by shape only — the key itself is never read, printed or
    logged (G14)."""
    if not settings.llm_configured:
        refuse("OPENROUTER_API_KEY is empty. Put it in .env and recreate the "
               "service first; this script never takes a key as an argument.")
    if settings.llm_classifier_daily_call_cap < 1:
        refuse(f"LLM_CLASSIFIER_DAILY_CALL_CAP is "
               f"{settings.llm_classifier_daily_call_cap}: no call is permitted.")
    if ".invalid" in settings.llm_base_url:
        refuse(f"LLM_BASE_URL is {settings.llm_base_url} — this is the dev twin. "
               "Run the live check in tf-ai-agent, not tf-ai-agent-dev.")


async def one_batch(provider, redis, memory_cap, memory_cost, headlines, label):
    print(f"\n{'=' * 70}\n{label}: {len(headlines)} headline(s)\n{'=' * 70}")
    results, result = await classifier.classify(
        provider, redis, memory_cap, memory_cost, headlines,
        model=settings.llm_model_classifier,
        cap=settings.llm_classifier_daily_call_cap,
    )
    for headline, classification in zip(headlines, results):
        print(f"\n  {headline['title']}")
        print(f"    {json.dumps({k: v for k, v in classification.items() if k != 'cached'})}")
    if result is None:
        print("\n  (everything was already cached — no call was made)")
        return 0.0
    print(f"\n  model        {result.model}")
    print(f"  finish       {result.finish_reason}")
    print(f"  duration_ms  {result.duration_ms}")
    print(f"  usage        {json.dumps(result.usage)}")
    return float(result.usage.get("cost") or 0.0)


async def main() -> None:
    preflight()
    print(f"base_url   {settings.llm_base_url}")
    print(f"model      {settings.llm_model_classifier}")
    print(f"cap        {settings.llm_classifier_daily_call_cap}/day")
    print("key        present (checked by shape only)")

    try:
        redis = await cache.create_redis()
    except Exception as e:
        print(f"Redis unavailable ({type(e).__name__}); running without cache")
        redis = None

    memory_cap, memory_cost = cache.MemoryCap(), cache.MemoryCost()
    provider = build_provider(settings, redis)

    spent = await one_batch(provider, redis, memory_cap, memory_cost, ONE, "STEP 1 — one headline")

    print("\n\nStep 1 succeeded. Continue to five headlines? [y/N] ", end="")
    if input().strip().lower() != "y":
        print("Stopped after one headline.")
    else:
        spent += await one_batch(provider, redis, memory_cap, memory_cost, FIVE,
                                 "STEP 2 — five headlines")

    print(f"\n{'=' * 70}\nTOTAL REPORTED COST: ${spent:.6f}\n{'=' * 70}")

    if redis is not None:
        usage = await cache.read_usage(
            redis,
            {cache.STATE_LLM_CALLS: memory_cap,
             cache.STATE_CLASSIFIER_CALLS: memory_cap},
            memory_cost,
        )
        print(f"GET /usage would now report: {json.dumps(usage)}")
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
