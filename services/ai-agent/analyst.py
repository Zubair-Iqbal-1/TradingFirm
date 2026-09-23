"""
TradingFirm — POST /analyze, the orchestration (Part 4.4).

Everything that fetches, spends or stores. The judgement-free parts (entry,
projection, prompt, schema, fingerprint, merge) are analyze.py's and are pure.

The order is the spec's, and each step is placed so that nothing is spent
that cannot be used:

    validate -> pool -> settings -> dossier -> risk-shield -> classify +
    write-back -> events -> plan math -> fingerprint -> cache -> lock ->
    verdict call -> merge -> store (verdict + ledger row, one transaction)
    -> cost + cache -> unlock
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import analyze
import cache
import classifier
import db
import events as events_mod
import ledger
import prompts
import upstream
from grading.plan_math import PLAN_MATH_VERSION, PlanMath, compute_plan
from providers.base import (
    LLMCapExceeded,
    LLMCooledDown,
    LLMError,
    LLMNotConfigured,
    LLMRateLimited,
    LLMUnavailable,
)
from tickers import validate_ticker

logger = logging.getLogger(__name__)


class AnalyzeError(Exception):
    """An HTTP status and a detail. Details carry a type, a rule or a count —
    never a body, a URL or a key (G14)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _memory(state, name):
    caps = getattr(state, "memory_caps", None) or {}
    return caps.get(name) or cache.MemoryCap()


def _unlabelled(items: list[dict]) -> list[dict]:
    return [i for i in items
            if isinstance(i, dict) and not isinstance(i.get("sentiment"), dict)
            and isinstance(i.get("headline"), str) and i["headline"].strip()]


async def _classify_news(state, settings, ticker: str, user_id: str, items: list[dict], now) -> dict:
    """Label the ticker's unlabelled headlines through 4.2's classifier,
    in-process, and write every one with an id back. Labelled rows are never
    sent. At most one LLM call (the dossier's 30-headline cap is the batch
    size). Fails OPEN: on any refusal the verdict runs on raw headlines."""
    summary = {"calls": 0, "classified": 0, "cached": 0, "writtenBack": 0,
               "writeBackErrors": 0, "ok": True}
    todo = _unlabelled(items)[:classifier.BATCH_MAX]
    if not todo:
        return summary

    headlines = [{
        "id": i.get("id") if isinstance(i.get("id"), int) else None,
        "title": i["headline"][:classifier.TITLE_MAX].replace("\x00", " "),
        "url": i.get("url"), "source": i.get("source"),
        "publishedAt": i.get("publishedAt"), "summary": i.get("summary"), "ticker": ticker,
    } for i in todo]
    pool, redis = getattr(state, "db_pool", None), getattr(state, "redis", None)
    try:
        results, result = await classifier.classify(
            state.provider, redis, _memory(state, cache.STATE_CLASSIFIER_CALLS),
            getattr(state, "memory_cost", None) or cache.MemoryCost(), headlines,
            model=settings.llm_model_classifier, cap=settings.llm_classifier_daily_call_cap,
            now=now, known_event_keys=events_mod.known_keys(items),
        )
    except (LLMError, classifier.ClassifierError, prompts.PromptMissing, ValueError) as e:
        await classifier.record_failure(
            pool, redis, e, route=ledger.ROUTE_ANALYZE, model=settings.llm_model_classifier,
            ticker=ticker, user_id=user_id,
        )
        logger.warning(f"/analyze {ticker}: classifier unavailable ({type(e).__name__}); "
                       f"{len(todo)} headline(s) stay unlabelled")
        summary["ok"] = False
        return summary

    if result is not None:
        summary["calls"] = 1
        await classifier.record_success(pool, redis, result, route=ledger.ROUTE_ANALYZE,
                                        ticker=ticker, user_id=user_id)
    summary["cached"] = sum(1 for c in results if c["cached"])
    summary["classified"] = len(results) - summary["cached"]
    summary["writtenBack"], summary["writeBackErrors"] = await classifier.write_back(
        getattr(state, "http", None), settings.data_engine_url,
        [h["id"] for h in headlines], results,
    )
    for item, label in zip(todo, results):
        item["sentiment"] = {k: v for k, v in label.items() if k != "cached"}
    return summary


def _verdict_body(row: dict) -> dict:
    return {
        "verdict": row["verdict"], "confidence": row["confidence"], "reasoning": row["reasoning"],
        "thesis": row["thesis"], "thesisBreakers": row["thesis_breakers"],
        "plan": row["plan_proposed"], "riskFlags": row["risk_flags"] or [],
    }


def _llm_status(error: LLMError) -> int:
    if isinstance(error, (LLMCapExceeded, LLMCooledDown, LLMRateLimited)):
        return 429
    if isinstance(error, (LLMNotConfigured, LLMUnavailable)):
        return 503
    return 502


async def run(state, settings, ticker: str, horizon: str, entry: Optional[float],
              fresh: bool = False, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    user_id = db.DEV_USER_ID                      # D18: one fixed user until auth exists

    # 1. input ────────────────────────────────────────────────────
    try:
        ticker = validate_ticker(ticker)
        if horizon not in upstream.HORIZONS:
            raise ValueError(f"horizon must be one of {upstream.HORIZONS}")
        given = analyze.parse_entry(entry)
    except ValueError as e:
        raise AnalyzeError(422, str(e)) from None

    # 2–3. a verdict that cannot be stored or sized is not asked for ─
    pool, redis = getattr(state, "db_pool", None), getattr(state, "redis", None)
    provider = getattr(state, "provider", None)
    if pool is None or provider is None:
        raise AnalyzeError(503, "database unavailable" if pool is None else "provider unavailable")
    try:
        account = await db.get_settings(pool, user_id)
    except db.DB_FAILURES as e:
        logger.warning(f"/analyze {ticker}: settings read failed ({type(e).__name__})")
        raise AnalyzeError(503, "database unavailable") from None
    if account is None or account["accountSize"] is None:
        raise AnalyzeError(409, "settings missing: set users.settings.account_size (docs/runbook.md)")

    # 4–5. inputs ─────────────────────────────────────────────────
    http = getattr(state, "http", None)
    try:
        dossier = await upstream.fetch_dossier(
            http, settings.data_engine_url, ticker, horizon, settings.dossier_timeout)
    except upstream.DossierNotFound as e:
        raise AnalyzeError(404, str(e)) from None
    except upstream.DossierUnavailable as e:
        raise AnalyzeError(503, str(e)) from None
    except upstream.DossierInvalid as e:
        raise AnalyzeError(502, str(e)) from None
    macro = await upstream.fetch_macro(http, settings.risk_shield_url, settings.risk_shield_timeout)

    # 6–7. news -> labels -> events ───────────────────────────────
    sections = dossier["sections"]
    news_items = [i for i in ((sections.get("news") or {}).get("items") or []) if isinstance(i, dict)]
    news = await _classify_news(state, settings, ticker, user_id, news_items, now)
    grouped = events_mod.group(news_items)

    # 8. plan math ────────────────────────────────────────────────
    indicators = sections["indicators"]
    zones = indicators.get("zones") or {}
    resolved, entry_source = analyze.resolve_entry(indicators, given)
    # `lastSwingLow` is not in the dossier yet (4.8a-de adds it); the
    # pass-through means plan math needs no edit when it arrives.
    swing = indicators.get("lastSwingLow")
    swing = swing if isinstance(swing, dict) else {}
    try:
        plan = compute_plan(
            entry=float(resolved), atr=indicators.get("atr14"),
            zones=analyze.tagged_zones(zones),
            account=float(account["accountSize"]), risk_pct=float(account["riskPct"]),
            ema20=indicators.get("ema20"), swing_low=swing.get("price"), swing_low_date=swing.get("date"),
        )
    except ValueError as e:
        raise AnalyzeError(502, f"dossier breaks the plan-math contract: {e}") from None
    has_plan = isinstance(plan, PlanMath)

    # 9. fingerprint and cache ────────────────────────────────────
    today = date.fromisoformat(cache.et_day(now))
    next_date, in_days = analyze.next_earnings(dossier, today)
    system = prompts.load(prompts.VERDICT)
    suffix = cache.verdict_suffix(user_id, ticker, horizon, analyze.entry_key(given))
    held = await cache.get_cached_verdict(redis, suffix)

    def fingerprint_from(reference_entry) -> str:
        return analyze.fingerprint(
            high_event_keys=events_mod.high_relevance_keys(grouped), next_earnings_date=next_date,
            regime=macro.get("regime"), brief_id=macro.get("briefId"),
            entry_key_=analyze.entry_key(given), as_of=dossier.get("asOf"),
            bucket=analyze.price_bucket(indicators["close"], reference_entry, indicators.get("atr14")),
            account=account["accountSize"], risk_pct=account["riskPct"],
            prompt_sha_=analyze.prompt_sha(system), model=settings.llm_model,
        )

    common = {
        "ticker": ticker, "horizon": horizon, "newsClassified": news["ok"], "classifier": news,
        "macroStatus": macro.get("status"), "regime": macro.get("regime"),
    }
    if held is not None and not fresh and held["fingerprint"] == fingerprint_from(held["entry"]):
        try:
            row = await db.get_verdict(pool, held["verdictId"], user_id)
        except db.DB_FAILURES as e:
            logger.warning(f"/analyze {ticker}: cached verdict read failed ({type(e).__name__})")
            row = None
        if row is not None:
            try:
                await db.bump_served(pool, held["verdictId"], now)
            except db.DB_FAILURES as e:
                logger.warning(f"/analyze {ticker}: served_count not bumped ({type(e).__name__})")
            logger.info(f"/analyze {ticker}: served verdict {held['verdictId']} from cache")
            return {
                **common, "cached": True, "stored": True, "verdictId": str(row["id"]),
                "askedAt": row["asked_at"].isoformat(), "entry": float(row["entry"]),
                "entrySource": row["entry_source"], "verdict": _verdict_body(row),
                "planRejection": row["plan_rejection"], "model": row["model"], "usage": {},
                "servedCount": (row.get("served_count") or 0) + 1,
            }

    # 10. one paid call per key at a time ─────────────────────────
    if not await cache.acquire_analyze_lock(redis, suffix):
        raise AnalyzeError(409, f"an analysis of {ticker} is already running")
    try:
        inputs = analyze.project(
            dossier, macro, grouped, plan, entry=resolved, entry_source=entry_source,
            today=today, news_classified=news["ok"],
        )
        # No `now` here: every ledger row is stamped when it is written, so a
        # request's rows sort in the order the calls happened (the classifier's
        # before the verdict's). The request's own time is ai.verdicts.asked_at.
        ledger_kw = dict(route=ledger.ROUTE_ANALYZE, label=analyze.LABEL, model=settings.llm_model,
                         counters=[cache.STATE_LLM_CALLS], ticker=ticker, user_id=user_id)

        # 11. the verdict call ────────────────────────────────────
        try:
            result = await provider.complete_structured(
                system, analyze.user_prompt(inputs),
                analyze.llm_schema(has_plan, extended=has_plan and plan.extended),
                label=analyze.LABEL, model=settings.llm_model,
                cache_system=settings.llm_verdict_cache,
            )
        except LLMError as e:
            await ledger.record_error(pool, redis, e, **ledger_kw)
            logger.warning(f"/analyze {ticker}: verdict call failed ({type(e).__name__})")
            raise AnalyzeError(_llm_status(e), f"{type(e).__name__}: {e}") from None
        except ValueError as e:
            logger.error(f"/analyze {ticker}: bad request built by this service ({e})")
            raise AnalyzeError(500, f"ValueError: {e}") from None

        await cache.record_cost(redis, getattr(state, "memory_cost", None) or cache.MemoryCost(),
                                result.usage.get("cost"), now)

        # 12. the model's judgement + plan math's numbers ─────────
        try:
            verdict = analyze.merge(result.data, plan, in_days)
        except analyze.VerdictRejected as e:
            await ledger.record(pool, redis, ledger.build(
                outcome="bad_response", result=result, **ledger_kw))
            logger.error(f"/analyze {ticker}: unusable verdict ({e})")
            raise AnalyzeError(502, f"VerdictRejected: {e}") from None

        body = verdict.model_dump(by_alias=True)
        _, rejection = analyze.plan_view(plan)
        record = {
            "user_id": user_id, "ticker": ticker, "horizon": horizon, "asked_at": now,
            "entry": resolved, "entry_source": entry_source, "dossier": dossier,
            "prompt_inputs": inputs, "prompt_sha": analyze.prompt_sha(system),
            "fingerprint": fingerprint_from(resolved), "macro_brief_id": macro.get("briefId"),
            "regime": macro.get("regime"), "verdict": body["verdict"],
            "confidence": body["confidence"], "reasoning": body["reasoning"],
            "thesis": body["thesis"], "thesis_breakers": body["thesisBreakers"],
            "risk_flags": body["riskFlags"], "plan_proposed": body["plan"],
            "plan_rejection": rejection, "model": result.model,
            "tokens_in": result.usage.get("input"), "tokens_out": result.usage.get("output"),
            "plan_math_version": PLAN_MATH_VERSION,
        }

        # 13. store: the verdict and its ledger row, or neither ───
        call_row = ledger.build(outcome=ledger.OUTCOME_OK, result=result, **ledger_kw)
        verdict_id: Optional[str] = None
        try:
            verdict_id = await db.insert_verdict_with_call(pool, record, call_row)
        except Exception as e:
            # D5 says every verdict is stored. This one was paid for and
            # could not be, so the whole row goes to the log for a hand
            # backfill (docs/runbook.md), and the answer is still returned.
            logger.error(
                f"/analyze {ticker}: VERDICT NOT STORED ({type(e).__name__}). "
                f"Backfill ai.verdicts + ai.llm_calls from this payload: "
                + json.dumps({"verdict": record, "llmCall": call_row}, default=str, sort_keys=True)
            )
            await ledger.missed(redis, now)

        # 14. cache only what points at a row ─────────────────────
        if verdict_id is not None:
            await cache.store_cached_verdict(redis, suffix, {
                "verdictId": verdict_id, "fingerprint": record["fingerprint"],
                "entry": str(resolved),
            }, settings.verdict_cache_ttl)

        logger.info(f"/analyze {ticker}: {body['verdict']} {body['confidence']} "
                    f"plan={'yes' if has_plan else rejection['reason']} stored={verdict_id is not None} "
                    f"cost={result.usage.get('cost')}")
        return {
            **common, "cached": False, "stored": verdict_id is not None, "verdictId": verdict_id,
            "askedAt": now.isoformat(), "entry": float(resolved), "entrySource": entry_source,
            "verdict": body, "planRejection": rejection, "model": result.model,
            "usage": result.usage, "servedCount": 0,
        }
    finally:
        await cache.release_analyze_lock(redis, suffix)
