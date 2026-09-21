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
import re
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

# Part 4.4: the event slug. One story from three sources gets one key, and the
# verdict prompt is handed one line per key (events.group). Two to eight
# lowercase words joined by hyphens — a charset that cannot carry markup, so a
# key can be echoed back into a later prompt as-is. data-engine keeps the same
# two values (SENTIMENT_EVENT_KEY_RE / _MAX) and accepts the key as optional.
EVENT_KEY_MAX = 80
EVENT_KEY_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+){1,7}$"
EVENT_KEY_RE = re.compile(EVENT_KEY_PATTERN)

# How many already-known keys a caller may offer for reuse. A model will not
# reliably re-invent the same slug in a later batch, so /analyze passes the
# keys already on the ticker's labelled headlines.
KNOWN_KEYS_MAX = 40

ITEM_LIMITS = {
    "relevance": RELEVANCE,
    "category": CATEGORIES,
    "sentiment": (-1.0, 1.0),
    "oneLine": ONE_LINE_MAX,
    "model": MODEL_MAX,
    "eventKey": (EVENT_KEY_MAX, EVENT_KEY_PATTERN),
}


def valid_event_key(value) -> bool:
    """The one check every event key goes through, in and out (G1.5)."""
    return (
        isinstance(value, str)
        and len(value) <= EVENT_KEY_MAX
        and EVENT_KEY_RE.match(value) is not None
    )

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
                "required": ["index", "relevance", "sentiment", "category", "oneLine", "eventKey"],
                "properties": {
                    "index": {"type": "integer"},
                    "relevance": {"type": "string", "enum": list(RELEVANCE)},
                    "sentiment": {"type": "number", "minimum": -1, "maximum": 1},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "oneLine": {"type": "string"},
                    "eventKey": {"type": "string"},
                },
            },
        },
    },
}


class ClassifierError(Exception):
    """Base. Messages carry a rule, a count or an error type — never a body."""


class BatchRejected(ClassifierError):
    """The answer broke the item contract. The whole batch is rejected.
    `result` is the paid-for LLMResult, set by classify() (Part 4.4)."""

    result: Optional[LLMResult] = None


# ── Prompt ───────────────────────────────────────────────────────

def user_prompt(headlines: list[dict], known_event_keys: Optional[list[str]] = None) -> str:
    """The numbered list the model answers against. `index` is the position
    in THIS list, which is what ties an answer back to a headline.

    `known_event_keys` are slugs already given to this ticker's stories; only
    ones passing valid_event_key reach the prompt, so the list cannot carry
    text of anyone's choosing."""
    known = [k for k in (known_event_keys or []) if valid_event_key(k)][:KNOWN_KEYS_MAX]
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
    reuse = ""
    if known:
        reuse = (
            "Event keys already in use for earlier headlines — reuse one, "
            "unchanged, when a headline is the same story:\n"
            + "\n".join(f"- {k}" for k in known) + "\n\n"
        )
    return (
        f"Classify these {len(headlines)} headlines. "
        f"Return exactly {len(headlines)} items, one per index.\n\n"
        + reuse
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

        # Last on purpose: every older rule keeps reporting its own reason.
        event_key = raw.get("eventKey")
        if not valid_event_key(event_key):
            raise BatchRejected(f"item {index}: eventKey is not a 2-8 word lowercase slug")

        by_index[index] = {
            "relevance": relevance,
            "sentiment": sentiment,
            "category": category,
            "oneLine": one_line.strip()[:ONE_LINE_MAX],
            "eventKey": event_key,
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
    known_event_keys: Optional[list[str]] = None,
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
            user_prompt(headlines, known_event_keys),
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
    known_event_keys: Optional[list[str]] = None,
) -> tuple[list[dict], Optional[LLMResult]]:
    """Classify `headlines`, returning (one classification per headline,
    the LLMResult or None when everything came from cache).

    Order: digest -> cache read -> one call for the misses -> validate ->
    cache write. Write-back is the route's job, because it covers cached
    items too (decision 6b).
    """
    digests = [cache.headline_digest(h["title"], h.get("url")) for h in headlines]
    cached = await cache.get_classifications(redis, sorted(set(digests)))
    # A label stored before Part 4.4 has no eventKey. It reads as a miss and
    # is classified once more, so every label the verdict sees can be grouped.
    cached = {d: c for d, c in cached.items() if valid_event_key(c.get("eventKey"))}

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
        provider, redis, memory_cap, to_send, model=model, cap=cap, now=now,
        known_event_keys=known_event_keys,
    )
    # The call is paid for whatever it answered, so its cost is counted
    # before the answer is judged (Part 4.4; 4.2 counted it after, which left
    # a rejected batch out of the day's total). A rejection carries the
    # result out with it, so the route can still write the ledger row.
    await cache.record_cost(redis, memory_cost, result.usage.get("cost"), now)
    try:
        answers = validate_answer(result.data, len(to_send))
    except BatchRejected as e:
        e.result = result
        raise

    classified_at = (now or datetime.now(timezone.utc)).isoformat()
    fresh = {
        digest: {**answer, "model": result.model[:MODEL_MAX], "classifiedAt": classified_at}
        for digest, answer in zip(wanted, answers)
    }
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


# ── Write-back ───────────────────────────────────────────────────

async def write_back(http, base_url: str, ids: list[Optional[int]], results: list[dict]) -> tuple[int, int]:
    """Write every classification whose item has an id to data-engine —
    cached or fresh (spec 4.2 decision 6b). Returns (written, errors).

    One function for both callers, /classify/headlines and /analyze
    (Part 4.4), so the "every item with an id" rule cannot drift between
    them. Fail-open: data_engine_client never raises.
    """
    import data_engine_client

    written, errors = 0, 0
    for news_id, classification in zip(ids, results):
        if news_id is None:
            continue
        payload = {k: v for k, v in classification.items() if k != "cached"}
        outcome = await data_engine_client.write_sentiment(http, base_url, news_id, payload)
        written += 1 if outcome.ok else 0
        errors += 0 if outcome.ok else 1
    return written, errors


# ── The ledger (Part 4.4) ────────────────────────────────────────
#
# Which day counters a classifier call counted against. Both, except the one
# post-wire error the reservation rule releases: LLMAuthFailed subclasses
# LLMNotConfigured, so _call_model gives the classifier's reservation back
# while the provider's global one stays. The ledger seeds both counters at
# startup, so its rows have to say the same thing the counters did.

COUNTERS = [cache.STATE_LLM_CALLS, cache.STATE_CLASSIFIER_CALLS]


async def record_success(pool, redis, result: LLMResult, *, route: str,
                         ticker: Optional[str] = None, user_id: Optional[str] = None) -> bool:
    import ledger
    return await ledger.record(pool, redis, ledger.build(
        route=route, label=LABEL, model=result.model, outcome=ledger.OUTCOME_OK,
        counters=COUNTERS, result=result, ticker=ticker, user_id=user_id,
    ))


async def record_failure(pool, redis, error: BaseException, *, route: str,
                         model: Optional[str] = None, ticker: Optional[str] = None,
                         user_id: Optional[str] = None) -> bool:
    """A ledger row for a classifier call that failed after the wire; nothing
    for one that never left. A rejected batch was answered and paid for, so
    its row carries the usage and reads `bad_response`."""
    import ledger
    from providers.base import LLMAuthFailed

    common = dict(route=route, label=LABEL, model=model or "unknown",
                  ticker=ticker, user_id=user_id)
    if isinstance(error, BatchRejected):
        return await ledger.record(pool, redis, ledger.build(
            outcome="bad_response", counters=COUNTERS, result=error.result, **common))
    outcome = ledger.outcome_of(error)
    if outcome is None:
        return False
    counters = [cache.STATE_LLM_CALLS] if type(error) is LLMAuthFailed else COUNTERS
    return await ledger.record(pool, redis, ledger.build(
        outcome=outcome, counters=counters, **common))
