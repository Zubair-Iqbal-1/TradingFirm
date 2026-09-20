"""
Part 4.2 — the classifier: the digest gate, the reservation rule, the item
contract and the cost record.

The provider is a stub object implementing complete_structured, so no HTTP
layer is involved at all here (the wire itself is 4.1's test file's job).
Nothing opens a socket and nothing calls an LLM.
"""

from datetime import datetime, timezone

import pytest

import cache
import classifier
from providers.base import (
    LLMAuthFailed,
    LLMBadResponse,
    LLMCapExceeded,
    LLMCooledDown,
    LLMNotConfigured,
    LLMRateLimited,
    LLMRefused,
    LLMRejected,
    LLMResult,
    LLMUnavailable,
)
from tests.test_cache import NOON, FakeRedis

MODEL = "anthropic/claude-sonnet-5"


def answer(n, cost=0.0057, model=MODEL, items=None):
    data = {"items": items if items is not None else [
        {"index": i, "relevance": "high", "sentiment": -0.4,
         "category": "guidance", "oneLine": f"Line {i}."} for i in range(n)
    ]}
    usage = {"input": 812, "output": 410}
    if cost is not None:
        usage["cost"] = cost
    return LLMResult(data=data, model=model, finish_reason="stop",
                     duration_ms=900, usage=usage)


class StubProvider:
    """Records every call; answers with `answers` in order, or raises."""

    def __init__(self, *answers, raises=None):
        self.answers = list(answers)
        self.raises = raises
        self.calls = []

    async def complete_structured(self, system, user, schema, **kw):
        self.calls.append({"system": system, "user": user, "schema": schema, **kw})
        if self.raises is not None:
            raise self.raises
        return self.answers.pop(0)


def heads(n, with_ids=True):
    return [{"title": f"Headline {i}", "url": f"https://x/{i}",
             "id": i if with_ids else None, "source": "Reuters",
             "publishedAt": None, "summary": None, "ticker": None}
            for i in range(n)]


async def run(provider, headlines, redis=None, cap=40, memory_cap=None, cost=None, now=NOON):
    return await classifier.classify(
        provider, redis, memory_cap or cache.MemoryCap(), cost or cache.MemoryCost(),
        headlines, model=MODEL, cap=cap, now=now,
    )


# ── The contract, pinned ─────────────────────────────────────────

def test_item_limits_pinned_to_spec():
    assert classifier.RELEVANCE == ("high", "medium", "low")
    assert classifier.CATEGORIES == (
        "guidance", "analyst", "legal", "product", "macro", "insider", "other")
    assert (classifier.ONE_LINE_MAX, classifier.MODEL_MAX) == (300, 100), (
        "data-engine's NewsSentimentRequest keeps a copy of this contract "
        "(services/data-engine/main.py, test_sentiment_contract_pinned_to_spec, "
        "spec 4.2 decision 10). Change both or neither."
    )
    assert classifier.BATCH_MAX == 30


def test_schema_is_acceptable_to_the_provider_and_matches_the_contract():
    from providers.base import validate_request
    validate_request("system", "user", classifier.SCHEMA, classifier.LABEL)

    item = classifier.SCHEMA["properties"]["items"]["items"]
    assert item["required"] == ["index", "relevance", "sentiment", "category", "oneLine"]
    assert item["properties"]["relevance"]["enum"] == list(classifier.RELEVANCE)
    assert item["properties"]["category"]["enum"] == list(classifier.CATEGORIES)
    assert item["properties"]["sentiment"]["minimum"] == -1
    assert item["properties"]["sentiment"]["maximum"] == 1


# ── One call per batch, and only for unseen headlines ────────────

@pytest.mark.asyncio
async def test_one_call_for_the_whole_batch():
    p = StubProvider(answer(30))
    out, result = await run(p, heads(30), redis=FakeRedis())

    assert len(p.calls) == 1, "a batch is one call, not one call per headline"
    assert len(out) == 30
    assert result.model == MODEL
    assert p.calls[0]["label"] == classifier.LABEL
    assert p.calls[0]["model"] == MODEL
    assert p.calls[0]["max_tokens"] == classifier.MAX_TOKENS


@pytest.mark.asyncio
async def test_all_cached_makes_no_llm_call():
    r = FakeRedis()
    p = StubProvider(answer(3))
    await run(p, heads(3), redis=r)
    assert len(p.calls) == 1

    p2 = StubProvider()   # would IndexError if it were called
    out, result = await run(p2, heads(3), redis=r)

    assert p2.calls == []
    assert result is None
    assert all(c["cached"] for c in out)
    assert [c["oneLine"] for c in out] == ["Line 0.", "Line 1.", "Line 2."]


@pytest.mark.asyncio
async def test_repeat_batch_is_served_from_cache():
    r = FakeRedis()
    first_p = StubProvider(answer(3))
    first, _ = await run(first_p, heads(3), redis=r)
    second, _ = await run(StubProvider(), heads(3), redis=r)

    strip = lambda rows: [{k: v for k, v in c.items() if k != "cached"} for c in rows]
    assert strip(first) == strip(second)
    assert [c["cached"] for c in first] == [False] * 3
    assert [c["cached"] for c in second] == [True] * 3


@pytest.mark.asyncio
async def test_only_the_unseen_are_sent():
    r = FakeRedis()
    await run(StubProvider(answer(2)), heads(2), redis=r)

    p = StubProvider(answer(2))
    out, _ = await run(p, heads(4), redis=r)

    assert len(p.calls) == 1
    sent = p.calls[0]["user"]
    assert "Headline 2" in sent and "Headline 3" in sent
    assert "Headline 0" not in sent and "Headline 1" not in sent
    assert [c["cached"] for c in out] == [True, True, False, False]


@pytest.mark.asyncio
async def test_duplicate_digests_collapse_within_batch():
    """The same story twice in one batch is one prompt line and one payment,
    and both items get the answer."""
    p = StubProvider(answer(1))
    same = [{"title": "Fed holds", "url": "https://r/x", "id": 1, "source": None,
             "publishedAt": None, "summary": None, "ticker": None},
            {"title": "FED HOLDS — live", "url": "https://R/X ", "id": 2, "source": None,
             "publishedAt": None, "summary": None, "ticker": None}]

    out, _ = await run(p, same, redis=FakeRedis())

    assert p.calls[0]["user"].count("[") == 1, "one prompt line, not two"
    assert len(out) == 2
    assert out[0]["oneLine"] == out[1]["oneLine"] == "Line 0."


@pytest.mark.asyncio
async def test_cache_read_failure_is_a_miss():
    p = StubProvider(answer(2))
    out, _ = await run(p, heads(2), redis=FakeRedis(raises=True))
    assert len(p.calls) == 1
    assert [c["cached"] for c in out] == [False, False]


@pytest.mark.asyncio
async def test_cache_write_failure_does_not_fail_request():
    p = StubProvider(answer(2))
    out, result = await run(p, heads(2), redis=FakeRedis(raises=True))
    assert result is not None and len(out) == 2


@pytest.mark.asyncio
async def test_no_redis_classifies_every_time():
    p = StubProvider(answer(2), answer(2))
    await run(p, heads(2), redis=None)
    await run(p, heads(2), redis=None)
    assert len(p.calls) == 2, "nothing is cached without Redis; it still works"


# ── The reservation rule ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_classifier_cap_exceeded_refuses_before_any_request():
    r, mem = FakeRedis(), cache.MemoryCap()
    r.store[cache.day_counter_key(NOON, name=cache.STATE_CLASSIFIER_CALLS)] = "40"
    p = StubProvider(answer(1))

    with pytest.raises(LLMCapExceeded) as excinfo:
        await run(p, heads(1), redis=r, cap=40, memory_cap=mem)

    assert p.calls == [], "no request went out"
    assert "no request was sent" in str(excinfo.value)
    assert r.store[cache.day_counter_key(NOON, name=cache.STATE_CLASSIFIER_CALLS)] == "40", (
        "the reservation is released: an over-cap refusal spent nothing"
    )


@pytest.mark.asyncio
async def test_classifier_counter_is_separate_from_the_global_one():
    r = FakeRedis()
    await run(StubProvider(answer(1)), heads(1), redis=r)
    assert r.store[cache.day_counter_key(NOON, name=cache.STATE_CLASSIFIER_CALLS)] == "1"
    assert cache.day_counter_key(NOON) not in r.store, (
        "the global counter is the provider's to move, not the classifier's"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    LLMNotConfigured("no key"),
    LLMAuthFailed("401"),
    LLMCooledDown("cooling after 429"),
    LLMCapExceeded("global cap"),
    ValueError("schema is wrong"),
])
async def test_pre_wire_refusals_release_the_reservation(error):
    """One rule: counted from the moment the request is sent. None of these
    reached the wire, so none of them costs a call."""
    r = FakeRedis()
    key = cache.day_counter_key(NOON, name=cache.STATE_CLASSIFIER_CALLS)

    with pytest.raises(type(error)):
        await run(StubProvider(raises=error), heads(1), redis=r)

    assert r.store[key] == "0", f"{type(error).__name__} must release"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    LLMRateLimited("this call got the 429"),
    LLMUnavailable("502"),
    LLMRejected("400"),
    LLMRefused("refusal of 120 chars"),
    LLMBadResponse("finish_reason length"),
])
async def test_post_wire_failures_keep_the_reservation(error):
    """A request that reached the wire counts against the day whatever it
    answered — 4.1's rule, unchanged."""
    r = FakeRedis()
    key = cache.day_counter_key(NOON, name=cache.STATE_CLASSIFIER_CALLS)

    with pytest.raises(type(error)):
        await run(StubProvider(raises=error), heads(1), redis=r)

    assert r.store[key] == "1", f"{type(error).__name__} must NOT release"


@pytest.mark.asyncio
async def test_cap_falls_back_to_memory_when_redis_is_down():
    mem = cache.MemoryCap()
    p = StubProvider(*[answer(1) for _ in range(3)])
    for _ in range(2):
        await run(p, heads(1), redis=FakeRedis(raises=True), cap=2, memory_cap=mem)

    assert mem.count("2026-09-20") == 2
    with pytest.raises(LLMCapExceeded):
        await run(p, heads(1), redis=FakeRedis(raises=True), cap=2, memory_cap=mem)


# ── The item contract ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("items, why", [
    ([], "too few"),
    ([{"index": 0, "relevance": "high", "sentiment": 0.1,
       "category": "guidance", "oneLine": "a"}] * 2, "duplicate index"),
    ([{"index": 5, "relevance": "high", "sentiment": 0.1,
       "category": "guidance", "oneLine": "a"}], "index out of range"),
    ([{"index": "0", "relevance": "high", "sentiment": 0.1,
       "category": "guidance", "oneLine": "a"}], "index not an integer"),
    ([{"index": 0, "relevance": "HIGH", "sentiment": 0.1,
       "category": "guidance", "oneLine": "a"}], "relevance enum"),
    ([{"index": 0, "relevance": "high", "sentiment": 0.1,
       "category": "rumour", "oneLine": "a"}], "category enum"),
    ([{"index": 0, "relevance": "high", "sentiment": 1.5,
       "category": "guidance", "oneLine": "a"}], "sentiment out of range"),
    ([{"index": 0, "relevance": "high", "sentiment": "bad",
       "category": "guidance", "oneLine": "a"}], "sentiment not a number"),
    ([{"index": 0, "relevance": "high", "sentiment": float("nan"),
       "category": "guidance", "oneLine": "a"}], "sentiment NaN"),
    ([{"index": 0, "relevance": "high", "sentiment": 0.1,
       "category": "guidance", "oneLine": "   "}], "blank oneLine"),
    ([{"index": 0, "relevance": "high", "sentiment": 0.1,
       "category": "guidance", "oneLine": "a\x00b"}], "NUL in oneLine"),
    (["not an object"], "item not an object"),
])
async def test_item_contract_violation_rejects_whole_batch(items, why):
    r = FakeRedis()
    p = StubProvider(answer(1, items=items))

    with pytest.raises(classifier.BatchRejected):
        await run(p, heads(1), redis=r)

    assert not any(k.startswith("tf:ai:classify:") for k in r.store), (
        f"{why}: nothing may be cached from a rejected batch"
    )


@pytest.mark.asyncio
async def test_items_not_a_list_is_rejected():
    with pytest.raises(classifier.BatchRejected):
        classifier.validate_answer({"items": {"index": 0}}, 1)
    with pytest.raises(classifier.BatchRejected):
        classifier.validate_answer({}, 1)


def test_validate_answer_reorders_by_index_and_trims():
    out = classifier.validate_answer({"items": [
        {"index": 1, "relevance": "low", "sentiment": 0, "category": "other",
         "oneLine": "  second  "},
        {"index": 0, "relevance": "high", "sentiment": -1, "category": "macro",
         "oneLine": "x" * 400},
    ]}, 2)

    assert [o["oneLine"] for o in out] == ["x" * 300, "second"]
    assert out[0]["sentiment"] == -1.0 and isinstance(out[0]["sentiment"], float)


def test_validate_answer_accepts_both_bounds_and_every_enum():
    items, expected = [], 0
    for relevance in classifier.RELEVANCE:
        for category in classifier.CATEGORIES:
            items.append({"index": expected, "relevance": relevance, "sentiment": 1.0,
                          "category": category, "oneLine": "ok"})
            expected += 1
    out = classifier.validate_answer({"items": items}, expected)
    assert len(out) == expected

    for bound in (-1.0, 0.0, 1.0):
        one = classifier.validate_answer({"items": [
            {"index": 0, "relevance": "low", "sentiment": bound,
             "category": "other", "oneLine": "ok"}]}, 1)
        assert one[0]["sentiment"] == bound


# ── Cost and stored shape ────────────────────────────────────────

@pytest.mark.asyncio
async def test_cost_is_recorded_against_the_day_and_month():
    r, cost = FakeRedis(), cache.MemoryCost()
    await run(StubProvider(answer(2, cost=0.0057)), heads(2), redis=r, cost=cost)

    assert float(r.store["tf:ai:state:cost_day:2026-09-20"]) == pytest.approx(0.0057)
    assert float(r.store["tf:ai:state:cost_month:2026-09"]) == pytest.approx(0.0057)


@pytest.mark.asyncio
async def test_missing_cost_counts_as_missing_not_zero():
    r, cost = FakeRedis(), cache.MemoryCost()
    await run(StubProvider(answer(1, cost=None)), heads(1), redis=r, cost=cost)

    assert r.store["tf:ai:state:cost_missing:2026-09-20"] == "1"
    assert "tf:ai:state:cost_day:2026-09-20" not in r.store


@pytest.mark.asyncio
async def test_stored_classification_carries_model_and_timestamp():
    r = FakeRedis()
    out, _ = await run(StubProvider(answer(1)), heads(1), redis=r)

    assert out[0]["model"] == MODEL
    assert out[0]["classifiedAt"] == NOON.isoformat()
    digest = cache.headline_digest("Headline 0", "https://x/0")
    stored = await cache.get_classifications(r, [digest])
    assert stored[digest]["model"] == MODEL
    assert set(stored[digest]) == {"relevance", "sentiment", "category", "oneLine",
                                   "model", "classifiedAt"}


@pytest.mark.asyncio
async def test_a_long_model_name_is_trimmed_to_the_contract():
    """data-engine refuses a model over 100 chars with a 422, and write-back
    is fail-open, so it would fail quietly. Trim before it gets there."""
    out, _ = await run(StubProvider(answer(1, model="m" * 250)), heads(1), redis=FakeRedis())
    assert len(out[0]["model"]) == classifier.MODEL_MAX


# ── The prompt ───────────────────────────────────────────────────

def test_user_prompt_numbers_every_headline_and_bounds_the_summary():
    text = classifier.user_prompt([
        {"title": "T0", "summary": "s" * 1000, "source": "Reuters",
         "ticker": "AAPL", "publishedAt": "2026-09-20T13:00:00+00:00"},
        {"title": "T1", "summary": None, "source": None, "ticker": None,
         "publishedAt": None},
    ])

    assert "[0] T0" in text and "[1] T1" in text
    assert "ticker: AAPL" in text and "source: Reuters" in text
    assert "s" * classifier.SUMMARY_MAX in text
    assert "s" * (classifier.SUMMARY_MAX + 1) not in text
    assert "Return exactly 2 items" in text


def test_user_prompt_bounds_a_huge_title():
    text = classifier.user_prompt([{"title": "t" * 5000}])
    assert "t" * classifier.TITLE_MAX in text
    assert "t" * (classifier.TITLE_MAX + 1) not in text
