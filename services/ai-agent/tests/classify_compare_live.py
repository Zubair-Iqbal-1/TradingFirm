"""
LIVE knob test: the same headlines classified by Sonnet 5 and by Haiku 4.5
(Part 4.8b-ai, spec 4.8b decision 12; spec 4.8b-ai decision 13).

*** THIS SCRIPT SPENDS REAL MONEY: two classifier calls per ticker. ***

It is not collected by pytest (pytest.ini restricts discovery to test_*.py) and
it must never run in CI. Run it by hand, once, in the PROD container, after a
go in chat, for the three tickers Zubair names:

    docker exec tf-ai-agent python3 tests/classify_compare_live.py OPCH RIOT AAL

Per ticker it reads the prod dossier over HTTP (data-engine), applies the
route's own pre-filter (events.prefilter, at most 15 headlines), builds the
same headline payload /analyze builds (analyst.headline_payload), and sends it
once to each model through classifier._call_model: **no classification-cache
read or write, no write-back**, cache_system off. Both calls go through both
caps and each writes one ledger row (route `classify`, label
`headline_classify_compare`), and its cost is counted.

It refuses to run without a key, on the dev twin (an `.invalid` base URL), or
when the day's remaining classifier budget (the cap minus today's counter) is
under 6 — the six calls it would make — before the first call.

Expected cost: 3 × ~$0.019 (Sonnet, uncached) + 3 × ~$0.010 (Haiku) ≈ $0.087.
The real figure is printed from OpenRouter's own usage.cost. Nothing here
prints the key or an account size.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx           # noqa: E402

import analyst         # noqa: E402
import cache           # noqa: E402
import classifier      # noqa: E402
import events          # noqa: E402
import ledger          # noqa: E402
import upstream        # noqa: E402
from config import settings   # noqa: E402

MODELS = ("anthropic/claude-sonnet-5", "anthropic/claude-haiku-4.5")
LABEL = "headline_classify_compare"     # the ledger row's label; the wire label stays classifier.LABEL
MIN_BUDGET = 6                          # three tickers × two models, refused before the first call


def refuse(reason: str) -> None:
    print(f"REFUSING to run the knob test: {reason}", file=sys.stderr)
    sys.exit(2)


async def remaining_budget(redis, memory_cap: cache.MemoryCap, now: Optional[datetime] = None) -> int:
    """The classifier cap minus today's classifier counter (Redis, else the
    in-process count)."""
    usage = await cache.read_usage(
        redis, {cache.STATE_LLM_CALLS: memory_cap, cache.STATE_CLASSIFIER_CALLS: memory_cap},
        cache.MemoryCost(), now)
    return settings.llm_classifier_daily_call_cap - int(usage["classifierCalls"])


def preflight(budget: Optional[int] = None) -> None:
    """Checked by shape only — the key itself is never read, printed or
    logged (G14). `budget` is remaining_budget()'s answer."""
    if not settings.llm_configured:
        refuse("OPENROUTER_API_KEY is empty. This script never takes a key as an argument.")
    if ".invalid" in settings.llm_base_url:
        refuse(f"LLM_BASE_URL is {settings.llm_base_url} — this is the dev twin. "
               "Run the knob test in tf-ai-agent, not tf-ai-agent-dev.")
    if budget is not None and budget < MIN_BUDGET:
        refuse(f"only {budget} classifier call(s) left today under the cap of "
               f"{settings.llm_classifier_daily_call_cap}; the test needs {MIN_BUDGET}.")


def agreement(a: list[dict], b: list[dict]) -> dict:
    """How far two answers to the same headlines agree: relevance, category,
    eventDate per item, and the event grouping as the set of index pairs
    that share a key."""
    n = len(a)

    def pairs(items):
        return {(i, j) for i in range(n) for j in range(i + 1, n)
                if items[i]["eventKey"] == items[j]["eventKey"]}
    same_pairs = pairs(a) & pairs(b)
    all_pairs = pairs(a) | pairs(b)
    return {
        "items": n,
        "relevance": sum(x["relevance"] == y["relevance"] for x, y in zip(a, b)),
        "category": sum(x["category"] == y["category"] for x, y in zip(a, b)),
        "eventDate": sum(x.get("eventDate") == y.get("eventDate") for x, y in zip(a, b)),
        "eventGroupingPairs": f"{len(same_pairs)} / {len(all_pairs)}",
        "differ": [i for i, (x, y) in enumerate(zip(a, b))
                   if (x["relevance"], x["category"], x.get("eventDate")) !=
                   (y["relevance"], y["category"], y.get("eventDate"))],
    }


async def compare_ticker(ticker: str, *, http, provider, redis, pool, memory_cap, memory_cost,
                         cap: int, models=MODELS, now: Optional[datetime] = None) -> dict:
    """One ticker: the dossier's headlines through the pre-filter, then one
    call per model. Returns {"headlines", "results": {model: {...}}}."""
    dossier = await upstream.fetch_dossier(http, settings.data_engine_url, ticker, "swing",
                                           settings.dossier_timeout)
    items = [i for i in ((dossier["sections"].get("news") or {}).get("items") or []) if isinstance(i, dict)]
    kept, cut = events.prefilter(items)
    headlines = analyst.headline_payload(kept, ticker)
    out = {"ticker": ticker, "headlines": headlines, "prefiltered": cut, "results": {}}
    if not headlines:
        return out
    for model in models:
        entry: dict = {"model": model}
        try:
            result = await classifier._call_model(
                provider, redis, memory_cap, headlines, model=model, cap=cap, now=now,
                cache_system=False,
            )
        except Exception as e:                       # a refusal or a wire failure: report, keep going
            entry["error"] = f"{type(e).__name__}: {e}"
            out["results"][model] = entry
            continue
        await cache.record_cost(redis, memory_cost, result.usage.get("cost"), now)
        entry["usage"] = result.usage
        entry["servedModel"] = result.model
        try:
            entry["answers"] = classifier.validate_answer(result.data, len(headlines))
            outcome = ledger.OUTCOME_OK
        except classifier.BatchRejected as e:
            entry["error"] = f"BatchRejected: {e}"
            outcome = "bad_response"
        await ledger.record(pool, redis, ledger.build(
            route=ledger.ROUTE_CLASSIFY, label=LABEL, model=model, outcome=outcome,
            counters=classifier.COUNTERS, result=result, ticker=ticker, now=now))
        out["results"][model] = entry
    return out


def print_report(report: dict) -> float:
    """The cost table and the agreement table for one ticker; returns the
    ticker's total reported cost."""
    print(f"\n{'=' * 70}\n{report['ticker']}: {len(report['headlines'])} headline(s) sent "
          f"({report['prefiltered']} pre-filtered away)\n{'=' * 70}")
    spent = 0.0
    for model, entry in report["results"].items():
        usage = entry.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        spent += cost
        print(f"  {model:32s} in={usage.get('input')} out={usage.get('output')} "
              f"cost=${cost:.6f} served={entry.get('servedModel')}"
              + (f"  ERROR {entry['error']}" if entry.get("error") else ""))
    answered = [m for m in MODELS if report["results"].get(m, {}).get("answers")]
    if len(answered) == 2:
        a, b = (report["results"][m]["answers"] for m in answered)
        agree = agreement(a, b)
        print(f"  agreement: relevance {agree['relevance']}/{agree['items']}, "
              f"category {agree['category']}/{agree['items']}, "
              f"eventDate {agree['eventDate']}/{agree['items']}, "
              f"eventKey grouping pairs {agree['eventGroupingPairs']}")
        for i in agree["differ"]:
            print(f"    [{i}] {report['headlines'][i]['title'][:90]}")
            for m, answers in zip(answered, (a, b)):
                x = answers[i]
                print(f"        {m.split('/')[-1]:18s} {x['relevance']:6s} {x['category']:9s} "
                      f"date={x.get('eventDate')} key={x['eventKey']}  {x['oneLine'][:80]}")
    return spent


async def main(tickers: list[str]) -> None:
    if not tickers:
        refuse("name the tickers: classify_compare_live.py OPCH RIOT AAL")
    try:
        redis = await cache.create_redis()
    except Exception as e:
        print(f"Redis unavailable ({type(e).__name__}); caps and totals from in-process state only")
        redis = None
    memory_cap, memory_cost = cache.MemoryCap(), cache.MemoryCost()
    budget = await remaining_budget(redis, memory_cap)
    preflight(budget)
    print(f"base_url   {settings.llm_base_url}")
    print(f"models     {MODELS}")
    print(f"budget     {budget} classifier call(s) left today; this run makes {2 * len(tickers)}")
    print("key        present (checked by shape only)")

    import db
    from providers import build_provider
    pool = await db.create_db_pool()
    provider = build_provider(settings, redis)
    total = 0.0
    async with httpx.AsyncClient() as http:
        for ticker in tickers:
            report = await compare_ticker(ticker, http=http, provider=provider, redis=redis, pool=pool,
                                          memory_cap=memory_cap, memory_cost=memory_cost,
                                          cap=settings.llm_classifier_daily_call_cap,
                                          now=datetime.now(timezone.utc))
            total += print_report(report)
    print(f"\n{'=' * 70}\nTOTAL REPORTED COST: ${total:.6f}\n{'=' * 70}")
    if redis is not None:
        usage = await cache.read_usage(
            redis, {cache.STATE_LLM_CALLS: memory_cap, cache.STATE_CLASSIFIER_CALLS: memory_cap}, memory_cost)
        print(f"GET /usage would now report: {json.dumps(usage)}")
        await redis.aclose()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main([t.strip().upper() for t in sys.argv[1:] if t.strip()]))
