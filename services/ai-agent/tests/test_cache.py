"""Parts 4.1 and 4.2 — the tf:ai: namespace, the ET day and month boundaries,
reserve/release, the headline digest, the cost totals, the classification
cache and the in-process fallbacks. Nothing here opens a socket."""

from datetime import datetime, timezone

import pytest

import cache


# ── Keys ─────────────────────────────────────────────────────────

def test_canonical_strips_and_uppers():
    assert cache.canonical("  openrouter ") == "OPENROUTER"
    with pytest.raises(ValueError):
        cache.canonical(None)


def test_every_key_lives_under_the_ai_prefix():
    keys = [
        cache.state_key("llm_calls"),
        cache.day_counter_key(datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)),
        cache.cooldown_key(cache.SOURCE_LLM),
    ]
    assert all(k.startswith("tf:ai:") for k in keys), keys
    assert not any(k.startswith(("tf:cache:", "tf:risk:")) for k in keys)


def test_state_key_is_case_and_space_insensitive():
    assert cache.state_key(" Llm_Calls ") == cache.state_key("llm_calls")
    assert cache.state_key("llm_calls") == "tf:ai:state:llm_calls"
    with pytest.raises(ValueError):
        cache.state_key("  ")


def test_cooldown_key_shape():
    assert cache.cooldown_key("openrouter") == "tf:ai:cooldown:OPENROUTER"


# ── The ET day boundary ──────────────────────────────────────────

def test_et_day_boundary_0330z_belongs_to_the_previous_et_day():
    """03:30Z on the 21st is 23:30 ET on the 20th — still the 20th's cap."""
    assert cache.et_day(datetime(2026, 9, 21, 3, 30, tzinfo=timezone.utc)) == "2026-09-20"
    assert cache.day_counter_key(datetime(2026, 9, 21, 3, 30, tzinfo=timezone.utc)) == (
        "tf:ai:state:llm_calls:2026-09-20"
    )


def test_et_day_rolls_over_at_et_midnight():
    assert cache.et_day(datetime(2026, 9, 21, 4, 1, tzinfo=timezone.utc)) == "2026-09-21"


def test_et_day_reads_a_naive_timestamp_as_utc():
    assert cache.et_day(datetime(2026, 9, 21, 3, 30)) == "2026-09-20"


# ── Fakes ────────────────────────────────────────────────────────

class FakeRedis:
    """Enough of redis.asyncio for the counter and the cooldown. `expire`
    honours nx the way Redis >= 7.0 does."""

    def __init__(self, raises=False):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.raises = raises
        self.expire_calls: list[tuple[str, int, bool]] = []

    def _check(self):
        if self.raises:
            raise RuntimeError("redis is down")

    async def incr(self, key):
        self._check()
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def decr(self, key):
        self._check()
        self.store[key] = str(int(self.store.get(key, "0")) - 1)
        return int(self.store[key])

    async def expire(self, key, ttl, nx=False):
        self._check()
        self.expire_calls.append((key, ttl, nx))
        if nx and key in self.ttls:
            return False
        self.ttls[key] = ttl
        return True

    async def set(self, key, value, ex=None):
        self._check()
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key):
        self._check()
        return self.store.get(key)

    async def ttl(self, key):
        self._check()
        return self.ttls.get(key, -2)

    async def mget(self, keys):
        self._check()
        return [self.store.get(k) for k in keys]

    async def incrbyfloat(self, key, amount):
        self._check()
        self.store[key] = str(float(self.store.get(key, "0")) + float(amount))
        return float(self.store[key])


# ── The daily counter ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reserve_call_increments_and_sets_the_ttl_once():
    r, mem = FakeRedis(), cache.MemoryCap()
    now = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    assert await cache.reserve_call(r, mem, now) == 1
    assert await cache.reserve_call(r, mem, now) == 2
    key = cache.day_counter_key(now)
    assert r.expire_calls == [(key, cache.TTL_DAY_COUNTER, True)] * 2
    assert r.ttls[key] == cache.TTL_DAY_COUNTER


@pytest.mark.asyncio
async def test_expire_nx_leaves_the_ttl_unchanged_on_the_second_incr():
    """NX is the whole point: a later call in the same day must not push the
    expiry forward, or a busy day's key never expires."""
    r, mem = FakeRedis(), cache.MemoryCap()
    now = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    key = cache.day_counter_key(now)
    await cache.reserve_call(r, mem, now)
    r.ttls[key] = 42                      # time has passed
    await cache.reserve_call(r, mem, now)
    assert r.ttls[key] == 42


@pytest.mark.asyncio
async def test_release_call_decrements():
    r, mem = FakeRedis(), cache.MemoryCap()
    now = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    await cache.reserve_call(r, mem, now)
    await cache.reserve_call(r, mem, now)
    await cache.release_call(r, mem, now)
    assert r.store[cache.day_counter_key(now)] == "1"


@pytest.mark.asyncio
async def test_counter_falls_back_to_memory_when_redis_raises():
    r, mem = FakeRedis(raises=True), cache.MemoryCap()
    now = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    assert await cache.reserve_call(r, mem, now) == 1
    assert await cache.reserve_call(r, mem, now) == 2
    assert mem.count("2026-09-20") == 2


@pytest.mark.asyncio
async def test_counter_uses_memory_when_redis_is_none():
    mem = cache.MemoryCap()
    now = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)
    assert await cache.reserve_call(None, mem, now) == 1
    await cache.release_call(None, mem, now)
    assert mem.count("2026-09-20") == 0


def test_memory_cap_is_keyed_by_the_et_day():
    mem = cache.MemoryCap()
    mem.incr("2026-09-20")
    assert mem.count("2026-09-21") == 0
    assert mem.decr("2026-09-21") == 0      # never negative


# ── Cooldowns ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_records_and_returns_its_cause():
    r, mem = FakeRedis(), cache.MemoryCooldowns()
    await cache.start_cooldown(r, mem, cache.SOURCE_LLM, 900, "402")
    left, cause = await cache.cooldown_remaining(r, mem, cache.SOURCE_LLM, 900)
    assert left == 900 and cause == "402"
    assert r.store[cache.cooldown_key(cache.SOURCE_LLM)] == "402"


@pytest.mark.asyncio
async def test_cooldown_clear_when_no_key():
    r, mem = FakeRedis(), cache.MemoryCooldowns()
    assert await cache.cooldown_remaining(r, mem, cache.SOURCE_LLM, 900) == (None, None)
    # Redis raising with no in-memory clock to fall back to is clear, not an
    # error: the cooldown fails open by design.
    assert await cache.cooldown_remaining(
        FakeRedis(raises=True), None, cache.SOURCE_LLM, 900) == (None, None)


@pytest.mark.asyncio
async def test_cooldown_falls_back_to_memory_when_redis_raises():
    r, mem = FakeRedis(raises=True), cache.MemoryCooldowns()
    await cache.start_cooldown(r, mem, cache.SOURCE_LLM, 900, "429")
    left, cause = await cache.cooldown_remaining(r, mem, cache.SOURCE_LLM, 900)
    assert left is not None and cause == "429"


@pytest.mark.asyncio
async def test_cooldown_write_falls_back_to_memory_when_redis_raises():
    r, mem = FakeRedis(raises=True), cache.MemoryCooldowns()
    await cache.start_cooldown(r, mem, cache.SOURCE_LLM, 900, "401")
    assert mem.remaining("OPENROUTER", 900)[1] == "401"


def test_memory_cooldown_expires():
    mem = cache.MemoryCooldowns()
    mem.start("OPENROUTER", "429")
    assert mem.remaining("OPENROUTER", 0) == (None, None)


# ── Part 4.2: the headline digest (G1.5's one normalization) ─────

def test_digest_prefers_the_url_and_ignores_case_and_space():
    a = cache.headline_digest("Fed holds rates", "https://reuters.com/a")
    b = cache.headline_digest("FED HOLDS RATES — live updates", "  https://REUTERS.com/a ")
    assert a == b, "same url is the same article whatever the headline says"
    assert len(a) == 32 and a.islower()


def test_digest_falls_back_to_the_title_without_a_url():
    a = cache.headline_digest("Fed holds rates")
    b = cache.headline_digest("  fed holds rates  ", "")
    c = cache.headline_digest("Fed holds rates", None)
    assert a == b == c
    assert a != cache.headline_digest("Fed cuts rates")


def test_digest_refuses_a_blank_title_or_a_non_string_url():
    for bad in ("", "   ", None, 7):
        with pytest.raises(ValueError):
            cache.headline_digest(bad)
    with pytest.raises(ValueError):
        cache.headline_digest("ok", 7)


def test_digest_does_not_detect_cross_source_duplicates():
    """Spec 4.2 decision 20, pinned so it is a recorded limitation and not a
    surprise: the same event under three urls is three digests and three
    payments. The fix belongs to dossier assembly in 4.4."""
    reuters = cache.headline_digest("Fed holds rates", "https://reuters.com/x")
    cnbc = cache.headline_digest("Fed holds rates", "https://cnbc.com/y")
    assert reuters != cnbc


def test_classify_key_shape_and_prefix():
    d = cache.headline_digest("Fed holds rates", "https://reuters.com/a")
    assert cache.classify_key(d) == f"tf:ai:classify:{d}"
    assert cache.classify_key(d).startswith("tf:ai:")
    for bad in ("", "  ", None):
        with pytest.raises(ValueError):
            cache.classify_key(bad)


# ── Part 4.2: the second counter on the same code path ───────────

NOON = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)   # 12:00 ET


def test_named_day_counter_keys_are_separate_and_default_unchanged():
    assert cache.day_counter_key(NOON) == "tf:ai:state:llm_calls:2026-09-20"
    assert cache.day_counter_key(NOON, name=cache.STATE_CLASSIFIER_CALLS) == (
        "tf:ai:state:classifier_calls:2026-09-20"
    )
    assert cache.month_counter_key(NOON) == "tf:ai:state:cost_month:2026-09"
    assert cache.et_month(NOON) == "2026-09"
    assert cache.et_month(datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)) == "2026-09"


@pytest.mark.asyncio
async def test_classifier_counter_does_not_touch_the_global_one():
    r = FakeRedis()
    caps = {n: cache.MemoryCap() for n in
            (cache.STATE_LLM_CALLS, cache.STATE_CLASSIFIER_CALLS)}
    for _ in range(3):
        await cache.reserve_call(r, caps[cache.STATE_CLASSIFIER_CALLS], NOON,
                                 name=cache.STATE_CLASSIFIER_CALLS)
    await cache.reserve_call(r, caps[cache.STATE_LLM_CALLS], NOON)

    assert r.store["tf:ai:state:classifier_calls:2026-09-20"] == "3"
    assert r.store["tf:ai:state:llm_calls:2026-09-20"] == "1"
    assert all(ttl == cache.TTL_DAY_COUNTER for _, ttl, _ in r.expire_calls)


@pytest.mark.asyncio
async def test_classifier_cap_falls_back_to_memory():
    r, mem = FakeRedis(raises=True), cache.MemoryCap()
    first = await cache.reserve_call(r, mem, NOON, name=cache.STATE_CLASSIFIER_CALLS)
    second = await cache.reserve_call(r, mem, NOON, name=cache.STATE_CLASSIFIER_CALLS)
    assert (first, second) == (1, 2)
    assert mem.count("2026-09-20") == 2

    await cache.release_call(r, mem, NOON, name=cache.STATE_CLASSIFIER_CALLS)
    assert mem.count("2026-09-20") == 1


# ── Part 4.2: cost totals ────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_cost_adds_to_day_and_month_with_the_40_day_ttl():
    r, mem = FakeRedis(), cache.MemoryCost()
    await cache.record_cost(r, mem, 0.0057, NOON)
    await cache.record_cost(r, mem, 0.0043, NOON)

    assert float(r.store["tf:ai:state:cost_day:2026-09-20"]) == pytest.approx(0.01)
    assert float(r.store["tf:ai:state:cost_month:2026-09"]) == pytest.approx(0.01)
    assert r.ttls["tf:ai:state:cost_day:2026-09-20"] == cache.TTL_COST == 3456000
    assert r.ttls["tf:ai:state:cost_month:2026-09"] == cache.TTL_COST


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "free", float("nan"), float("inf")])
async def test_missing_cost_counts_as_missing_not_zero(bad):
    r, mem = FakeRedis(), cache.MemoryCost()
    await cache.record_cost(r, mem, bad, NOON)
    assert r.store["tf:ai:state:cost_missing:2026-09-20"] == "1"
    assert "tf:ai:state:cost_day:2026-09-20" not in r.store
    assert r.ttls["tf:ai:state:cost_missing:2026-09-20"] == cache.TTL_COST


@pytest.mark.asyncio
async def test_cost_write_failure_does_not_fail_request():
    """record_cost never raises: the classification is already paid for."""
    r, mem = FakeRedis(raises=True), cache.MemoryCost()
    await cache.record_cost(r, mem, 0.0057, NOON)
    await cache.record_cost(r, mem, None, NOON)
    assert mem.total("2026-09-20") == pytest.approx(0.0057)
    assert mem.total("2026-09") == pytest.approx(0.0057)
    assert mem.missing("2026-09-20") == 1


@pytest.mark.asyncio
async def test_read_usage_from_redis_and_from_memory():
    r = FakeRedis()
    caps = {cache.STATE_LLM_CALLS: cache.MemoryCap(),
            cache.STATE_CLASSIFIER_CALLS: cache.MemoryCap()}
    cost = cache.MemoryCost()
    await cache.reserve_call(r, caps[cache.STATE_LLM_CALLS], NOON)
    await cache.reserve_call(r, caps[cache.STATE_CLASSIFIER_CALLS], NOON,
                             name=cache.STATE_CLASSIFIER_CALLS)
    await cache.record_cost(r, cost, 0.0057, NOON)

    live = await cache.read_usage(r, caps, cost, NOON)
    assert live == {"day": "2026-09-20", "month": "2026-09", "source": "redis",
                    "llmCalls": 1, "classifierCalls": 1, "costMissing": 0,
                    "costToday": 0.0057, "costMonth": 0.0057}

    empty = await cache.read_usage(FakeRedis(), caps, cost, NOON)
    assert empty["llmCalls"] == 0 and empty["costToday"] == 0.0


@pytest.mark.asyncio
async def test_read_usage_falls_back_to_memory_when_redis_raises():
    caps = {cache.STATE_LLM_CALLS: cache.MemoryCap(),
            cache.STATE_CLASSIFIER_CALLS: cache.MemoryCap()}
    cost = cache.MemoryCost()
    caps[cache.STATE_LLM_CALLS].incr("2026-09-20")
    cost.add("2026-09-20", "2026-09", 0.25)
    cost.add_missing("2026-09-20")

    for r in (None, FakeRedis(raises=True)):
        out = await cache.read_usage(r, caps, cost, NOON)
        assert out["source"] == "memory"
        assert out["llmCalls"] == 1 and out["classifierCalls"] == 0
        assert out["costToday"] == 0.25 and out["costMonth"] == 0.25
        assert out["costMissing"] == 1


# ── Part 4.2: the classification cache ───────────────────────────

CLASSIFICATION = {"relevance": "high", "sentiment": -0.4, "category": "guidance",
                  "oneLine": "Guidance cut.", "model": "m",
                  "classifiedAt": "2026-09-20T16:00:00+00:00"}


@pytest.mark.asyncio
async def test_store_then_get_classifications_round_trip():
    r = FakeRedis()
    d1 = cache.headline_digest("a", "https://x/1")
    d2 = cache.headline_digest("b", "https://x/2")
    written = await cache.store_classifications(r, {d1: CLASSIFICATION})

    assert written == 1
    assert r.ttls[cache.classify_key(d1)] == cache.TTL_CLASSIFY == 604800
    found = await cache.get_classifications(r, [d1, d2])
    assert found == {d1: CLASSIFICATION}


@pytest.mark.asyncio
async def test_cache_read_failure_is_a_miss():
    d = cache.headline_digest("a", "https://x/1")
    assert await cache.get_classifications(FakeRedis(raises=True), [d]) == {}
    assert await cache.get_classifications(None, [d]) == {}
    assert await cache.get_classifications(FakeRedis(), []) == {}


@pytest.mark.asyncio
async def test_unparseable_or_non_object_cached_value_is_a_miss():
    r = FakeRedis()
    d1 = cache.headline_digest("a", "https://x/1")
    d2 = cache.headline_digest("b", "https://x/2")
    r.store[cache.classify_key(d1)] = "{not json"
    r.store[cache.classify_key(d2)] = '["a list"]'
    assert await cache.get_classifications(r, [d1, d2]) == {}


@pytest.mark.asyncio
async def test_cache_write_failure_returns_zero_and_does_not_raise():
    d = cache.headline_digest("a", "https://x/1")
    assert await cache.store_classifications(FakeRedis(raises=True), {d: CLASSIFICATION}) == 0
    assert await cache.store_classifications(None, {d: CLASSIFICATION}) == 0
    assert await cache.store_classifications(FakeRedis(), {}) == 0


# ── Part 4.2: create_redis, which 4.1 shipped without a caller ───

@pytest.mark.asyncio
async def test_create_redis_bounds_both_timeouts_and_pings(monkeypatch):
    """The factory 4.1 could not cover, because nothing built a client. It
    is bounded on BOTH sides on purpose: redis-py's own defaults are
    unbounded, so a Redis that accepts the socket and never answers would
    otherwise hold startup open forever."""
    import redis.asyncio as aioredis

    calls = {}

    class FakeClient:
        def __init__(self):
            self.pinged = False

        async def ping(self):
            self.pinged = True
            return True

    built = FakeClient()

    def fake_from_url(url, **kwargs):
        calls["url"] = url
        calls.update(kwargs)
        return built

    monkeypatch.setattr(aioredis, "from_url", fake_from_url)

    client = await cache.create_redis("redis://redis:6379/1", timeout=2.5)

    assert client is built
    assert built.pinged is True, "an unverified connection is not a connection"
    assert calls["url"] == "redis://redis:6379/1"
    assert calls["decode_responses"] is True
    assert calls["socket_connect_timeout"] == 2.5
    assert calls["socket_timeout"] == 2.5


@pytest.mark.asyncio
async def test_create_redis_defaults_to_settings_url_and_startup_timeout(monkeypatch):
    """STARTUP_TIMEOUT is read at call time, never bound at import, so a
    test can move it. The lifespan relies on that."""
    import config
    import redis.asyncio as aioredis

    calls = {}

    class FakeClient:
        async def ping(self):
            return True

    monkeypatch.setattr(config, "STARTUP_TIMEOUT", 0.25)
    monkeypatch.setattr(aioredis, "from_url",
                        lambda url, **kw: (calls.update(url=url, **kw), FakeClient())[1])

    await cache.create_redis()

    assert calls["url"] == cache.settings.redis_url
    assert calls["socket_connect_timeout"] == 0.25
    assert calls["socket_timeout"] == 0.25


@pytest.mark.asyncio
async def test_create_redis_propagates_a_failed_ping(monkeypatch):
    """The lifespan is what turns this into a warning and a None; the
    factory itself must not swallow it."""
    import redis.asyncio as aioredis

    class DeadClient:
        async def ping(self):
            raise ConnectionError("refused")

    monkeypatch.setattr(aioredis, "from_url", lambda url, **kw: DeadClient())

    with pytest.raises(ConnectionError):
        await cache.create_redis("redis://nowhere:6379")
