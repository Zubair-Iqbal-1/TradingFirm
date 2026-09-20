"""Part 4.1 — the tf:ai: namespace, the ET day boundary, reserve/release and
the in-process fallbacks. Nothing here opens a socket."""

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
