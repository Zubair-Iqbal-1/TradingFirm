"""
TradingFirm — the headline classifier (Part 4.2).

One batch of up to 30 headlines becomes **one** structured LLM call. Anything
already classified inside the 7-day digest window is served from Redis and
never sent, so the model sees only what it has not seen (spec gate i).

Two things bound the spend, both before the wire:
  1. this module's own day counter, `classifier_calls`, smaller than the
     global one so a batch loop cannot eat the analyst's budget (gate ii);
  2. 4.1's global cap and post-refusal cooldown, inside the provider.

The reservation rule, stated once and obeyed in one place (`_call_model`):
**a call is counted from the moment the request is sent, and the reservation
is released on every refusal where no request reached the wire.** Without it
a missing key would silently burn the classifier's whole day at the first
refusal.

An answer that breaks the item contract rejects the **whole batch** — never
repaired, never re-asked, never partially accepted (decision 5). The provider
only guarantees the top-level `required` keys are present; every rule below
that is checked here.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import cache
import prompts
from providers.base import (
    LLMCapExceeded,
    LLMCooledDown,
    LLMNotConfigured,
    LLMProvider,
    LLMResult,
)

logger = logging.getLogger(__name__)

# ── The contract ─────────────────────────────────────────────────
#
# This is the copy ai-agent keeps; data-engine keeps the other one as
# NewsSentimentRequest (spec decision 10). test_item_limits_pinned_to_spec
# here and test_sentiment_contract_pinned_to_spec there are what hold them
# together. Change both or neither: a drift is a 422 on every write-back, and
# write-back is fail-open, so it would fail quietly.

RELEVANCE = ("high", "medium", "low")
CATEGORIES = ("guidance", "analyst", "legal", "product", "macro", "insider", "other")
ONE_LINE_MAX = 300          # data-engine's SENTIMENT_ONE_LINE_MAX
MODEL_MAX = 100             # data-engine's SENTIMENT_MODEL_MAX

ITEM_LIMITS = {
    "relevance": RELEVANCE,
    "category": CATEGORIES,
    "sentiment": (-1.0, 1.0),
    "oneLine": ONE_LINE_MAX,
    "model": MODEL_MAX,
}

# Batch size. One call per batch, so this is also the coarseness of the spend.
BATCH_MAX = 30

# What the model is told about each headline. The summary is an input budget,
# not a truncated answer: a 10,000-character summary would cost more than the
# classification is worth and says nothing the first 300 characters do not.
SUMMARY_MAX = 300
TITLE_MAX = 1000

# The label names the call in the log AND becomes
# response_format.json_schema.name, so it must match LABEL_RE.
LABEL = "headline_classify"

# Output budget for one batch: 30 one-line answers plus reasoning at effort
# "low". Well under the 8,000 default, because this call never needs it.
MAX_TOKENS = 4000

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "relevance", "sentiment", "category", "oneLine"],
                "properties": {
                    "index": {"type": "integer"},
                    "relevance": {"type": "string", "enum": list(RELEVANCE)},
                    "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "oneLine": {"type": "string"},
                },
            },
        },
    },
}


class ClassifierError(Exception):
    """Base. Messages carry a rule, a count or an error type — never a body."""


class BatchRejected(ClassifierError):
    """The answer broke the item contract. The whole batch is rejected."""


# ── Prompt ───────────────────────────────────────────────────────

def user_prompt(headlines: list[dict]) -> str:
    """The numbered list the model answers against. `index` is the position
    in THIS list, which is what ties an answer back to a headline."""
    lines = []
    for index, h in enumerate(headlines):
        parts = [f"[{index}] {h['title'][:TITLE_MAX]}"]
        if h.get("ticker"):
            parts.append(f"    ticker: {h['ticker']}")
        if h.get("source"):
            parts.append(f"    source: {h['source']}")
        if h.get("publishedAt"):
            parts.append(f"    published: {h['publishedAt']}")
        summary = (h.get("summary") or "").strip()
        if summary:
            parts.append(f"    summary: {summary[:SUMMARY_MAX]}")
        lines.append("\n".join(parts))
    return (
        f"Classify these {len(headlines)} headlines. "
        f"Return exactly {len(headlines)} items, one per index.\n\n"
        + "\n\n".join(lines)
    )


# ── Answer validation ────────────────────────────────────────────

def validate_answer(data: dict, expected: int) -> list[dict]:
    """The answer's items in index order, or BatchRejected.

    Every rule here is load-bearing. The provider guarantees only that the
    top-level `required` keys exist; OpenRouter's own docs say `strict: true`
    is "not guaranteed on every endpoint". A half-trusted answer would put a
    wrong verdict into Postgres, which is worse than one rejected batch.
    """
    items = data.get("items")
    if not isinstance(items, list):
        raise BatchRejected("items is not a list")
    if len(items) != expected:
        raise BatchRejected(f"expected {expected} items, got {len(items)}")

    by_index: dict[int, dict] = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise BatchRejected("an item is not an object")
        index = raw.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise BatchRejected("an item has a non-integer index")
        if not 0 <= index < expected:
            raise BatchRejected(f"index {index} is outside 0..{expected - 1}")
        if index in by_index:
            raise BatchRejected(f"index {index} appears twice")

        relevance = raw.get("relevance")
        if relevance not in RELEVANCE:
            raise BatchRejected(f"item {index}: relevance is not one of {RELEVANCE}")

        category = raw.get("category")
        if category not in CATEGORIES:
            raise BatchRejected(f"item {index}: category is not one of {CATEGORIES}")

        sentiment = raw.get("sentiment")
        if isinstance(sentiment, bool) or not isinstance(sentiment, (int, float)):
            raise BatchRejected(f"item {index}: sentiment is not a number")
        sentiment = float(sentiment)
        if sentiment != sentiment or not -1.0 <= sentiment <= 1.0:
            raise BatchRejected(f"item {index}: sentiment is outside -1..1")

        one_line = raw.get("oneLine")
        if not isinstance(one_line, str) or not one_line.strip():
            raise BatchRejected(f"item {index}: oneLine is blank")
        if "\x00" in one_line:
            raise BatchRejected(f"item {index}: oneLine contains NUL")

        by_index[index] = {
            "relevance": relevance,
            "sentiment": sentiment,
            "category": category,
            "oneLine": one_line.strip()[:ONE_LINE_MAX],
        }

    return [by_index[i] for i in range(expected)]


# ── The model call ───────────────────────────────────────────────

async def _call_model(
    provider: LLMProvider,
    redis,
    memory_cap: cache.MemoryCap,
    headlines: list[dict],
    *,
    model: Optional[str],
    cap: int,
    now: Optional[datetime] = None,
) -> LLMResult:
    """One structured call, with the classifier's own reservation around it.

    The `finally`-free shape is deliberate: only the named pre-wire refusals
    release, and everything else keeps the reservation, because a request
    that reached the wire counts against the day whatever it answered.
    """
    count = await cache.reserve_call(
        redis, memory_cap, now, name=cache.STATE_CLASSIFIER_CALLS
    )
    if count > cap:
        await cache.release_call(
            redis, memory_cap, now, name=cache.STATE_CLASSIFIER_CALLS
        )
        raise LLMCapExceeded(
            f"classifier daily cap reached ({cap} calls); no request was sent"
        )

    try:
        return await provider.complete_structured(
            prompts.load(prompts.HEADLINE_CLASSIFY),
            user_prompt(headlines),
            SCHEMA,
            label=LABEL,
            model=model,
            max_tokens=MAX_TOKENS,
        )
    except (LLMNotConfigured, LLMCooledDown, LLMCapExceeded, ValueError):
        # Pre-wire, every one of them: no key (the client is not even built),
        # inside the cooldown window, over the global cap, or a request we
        # refused to send. Nothing was spent, so nothing is counted.
        await cache.release_call(
            redis, memory_cap, now, name=cache.STATE_CLASSIFIER_CALLS
        )
        raise


async def classify(
    provider: LLMProvider,
    redis,
    memory_cap: cache.MemoryCap,
    memory_cost: cache.MemoryCost,
    headlines: list[dict],
    *,
    model: Optional[str],
    cap: int,
    now: Optional[datetime] = None,
) -> tuple[list[dict], Optional[LLMResult]]:
    """Classify `headlines`, returning (one classification per headline,
    the LLMResult or None when everything came from cache).

    Order: digest -> cache read -> one call for the misses -> validate ->
    cache write. Write-back is the route's job, because it covers cached
    items too (decision 6b).
    """
    digests = [cache.headline_digest(h["title"], h.get("url")) for h in headlines]
    cached = await cache.get_classifications(redis, sorted(set(digests)))

    # One prompt line per *distinct* unseen digest: the same story twice in
    # one batch is one payment, and both items get the answer.
    wanted: list[str] = []
    for digest in digests:
        if digest not in cached and digest not in wanted:
            wanted.append(digest)

    if not wanted:
        logger.info(f"classify: {len(headlines)} headline(s), all from cache, no call")
        return [dict(cached[d], cached=True) for d in digests], None

    first_for = {}
    for digest, headline in zip(digests, headlines):
        first_for.setdefault(digest, headline)
    to_send = [first_for[d] for d in wanted]

    result = await _call_model(
        provider, redis, memory_cap, to_send, model=model, cap=cap, now=now
    )
    answers = validate_answer(result.data, len(to_send))

    classified_at = (now or datetime.now(timezone.utc)).isoformat()
    fresh = {
        digest: {**answer, "model": result.model[:MODEL_MAX], "classifiedAt": classified_at}
        for digest, answer in zip(wanted, answers)
    }
    await cache.record_cost(redis, memory_cost, result.usage.get("cost"), now)
    await cache.store_classifications(redis, fresh)

    logger.info(
        f"classify: {len(headlines)} headline(s), {len(cached)} cached, "
        f"{len(to_send)} sent, model={result.model}, "
        f"cost={result.usage.get('cost')}"
    )

    out = []
    for digest in digests:
        if digest in fresh:
            out.append(dict(fresh[digest], cached=False))
        else:
            out.append(dict(cached[digest], cached=True))
    return out, result
