"""
Part 4.4 — POST /analyze/{ticker}. All HTTP and the LLM are mocked: the
provider is a stub, data-engine and risk-shield sit behind one
httpx.MockTransport, Postgres is tests.fake_pool. Nothing opens a socket.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient

import cache
import db
import main
from providers.base import (
    LLMBadResponse, LLMCapExceeded, LLMCooledDown, LLMNotConfigured, LLMRateLimited,
    LLMRefused, LLMResult, LLMUnavailable,
)
from tests.fake_pool import FakePool
from tests.test_cache import FakeRedis

MODEL = "anthropic/claude-sonnet-5"
VERDICT_ID = "11111111-1111-4111-8111-111111111111"
ZONES = [{"low": 45.00, "high": 45.40}, {"low": 47.80, "high": 48.10},
         {"low": 53.90, "high": 54.30}, {"low": 56.90, "high": 57.30}]
LABEL = {"relevance": "high", "sentiment": -0.4, "category": "guidance", "oneLine": "Guidance cut.",
         "eventKey": "aapl-guidance-cut", "model": MODEL, "classifiedAt": "2026-09-20T18:00:00+00:00"}


def news_item(i, label=None, with_id=True):
    return {"id": 500 + i if with_id else None, "headline": f"Headline {i}", "url": f"https://x/{i}",
            "source": "Reuters", "publishedAt": f"2026-09-2{i % 2}T13:00:00Z", "summary": "s",
            "sentiment": label}


def dossier(news=(), close=50.0, atr=1.2, zones=ZONES, as_of="2026-09-18T00:00:00Z",
            earnings="2026-10-29T00:00:00Z", status="ok"):
    return {"ticker": "AAPL", "horizon": "swing", "asOf": as_of, "cached": False, "sections": {
        # ema20 47.5 → an EMA20 stop of 46.30, under the zone stop 46.60, so the
        # far-support rule keeps the zone stop and example A's numbers hold
        "indicators": {"status": status, "close": close, "atr14": atr, "ema20": 47.5,
                       "zones": {"support": list(zones[:2]), "resistance": list(zones[2:])}},
        "news": {"status": "ok", "items": list(news)},
        "events": {"status": "ok", "items": [{"type": "earnings", "at": earnings, "meta": {}}]},
        "earnings": {"status": "ok", "reactions": []},
        "filings": {"status": "ok", "rows": []},
        "recommendations": {"status": "ok", "items": []},
        "profile": {"status": "ok", "name": "Apple Inc", "industry": "Tech", "marketCap": 3e12},
    }}


HEALTH = {"score": 66, "regime": "CAUTIOUS", "trend": "stable", "stale": False,
          "checkedAt": "2026-09-21T16:55:00+00:00", "overlay": None, "weekend": None}


class World:
    """data-engine + risk-shield behind one MockTransport. Mutable, so a test
    can change what the next call sees."""

    def __init__(self):
        self.dossier = dossier()
        self.dossier_response = None          # an httpx.Response or an exception overrides
        self.health = httpx.Response(200, json=HEALTH)
        self.brief = httpx.Response(404, json={"detail": "no macro brief yet"})
        self.requests: list[tuple[str, str]] = []
        self.writes: list[tuple[str, dict]] = []

    def __call__(self, request):
        path = request.url.path
        self.requests.append((request.method, f"{request.url.host}{path}"))
        if path.startswith("/dossier/"):
            if isinstance(self.dossier_response, Exception):
                raise self.dossier_response
            return self.dossier_response or httpx.Response(200, json=self.dossier)
        if path == "/market/health":
            if isinstance(self.health, Exception):
                raise self.health
            return self.health
        if path == "/macro/brief":
            return self.brief
        if path.startswith("/news/") and request.method == "POST":
            self.writes.append((path, json.loads(request.content)))
            return httpx.Response(200, json={"id": 1, "updated": True})
        return httpx.Response(500)


class Provider:
    """Answers by label: `headline_classify` and `verdict`."""

    def __init__(self, verdict=None, raises=None, classify_raises=None):
        self.verdict = verdict or GO
        self.raises, self.classify_raises = raises, classify_raises
        self.calls: list[dict] = []

    def count(self, label):
        return sum(1 for c in self.calls if c["label"] == label)

    async def complete_structured(self, system, user, schema, **kw):
        self.calls.append({"system": system, "user": user, "schema": schema, **kw})
        if kw["label"] == "headline_classify":
            if self.classify_raises is not None:
                raise self.classify_raises
            n = int(user.split("Classify these ")[1].split(" ")[0])
            return LLMResult(data={"items": [
                {"index": i, "relevance": "high", "sentiment": -0.4, "category": "guidance",
                 "oneLine": f"Line {i}.", "eventKey": f"aapl-story-{i}"} for i in range(n)]},
                model=MODEL, finish_reason="stop", duration_ms=5,
                usage={"input": 900, "output": 300, "cost": 0.0048})
        if self.raises is not None:
            raise self.raises
        data = self.verdict
        if data is GO and "go" not in schema["properties"]["verdict"]["enum"]:
            data = WAIT                     # what strict decoding would force
        return LLMResult(data=data, model=MODEL, finish_reason="stop", duration_ms=9,
                         usage={"input": 6100, "output": 900, "reasoning": 300,
                                "cacheWrite": 1400, "cost": 0.0212}, host="Anthropic")


GO = {"verdict": "go", "confidence": 62, "reasoning": "Because.", "thesis": ["a", "b", "c"],
      "thesisBreakers": ["x"], "riskFlags": ["earnings in 38 days"],
      "invalidation": "daily close below the 20 EMA", "holdThroughEarnings": False, "horizonDays": 10}
WAIT = {k: v for k, v in GO.items() if k not in ("invalidation", "holdThroughEarnings", "horizonDays")}
WAIT["verdict"] = "wait"


class Store(FakePool):
    """FakePool that remembers the inserted verdict and serves it back."""

    def __init__(self, account=Decimal("25000"), settings_row=True, **kw):
        self.row = None
        self.served = 0
        settings = ({"account_size": account, "risk_per_trade_pct": Decimal("1.0")}
                    if settings_row else None)
        super().__init__({
            "users.settings": settings,
            "INSERT INTO ai.verdicts": self._insert,
            "FROM ai.verdicts": lambda args: self._select(),
            "served_count = served_count + 1": self._bump,
        }, **kw)

    def _insert(self, args):
        row = dict(zip(db.VERDICT_COLUMNS, args))
        self.row = {"id": VERDICT_ID, **row, "served_count": 0}
        return {"id": VERDICT_ID}

    def _select(self):
        if self.row is None:
            return None
        return {**self.row, "served_count": self.served}

    def _bump(self, args):
        self.served += 1

    def ledger(self):
        return [dict(zip(db.LLM_CALL_COLUMNS, c[2])) for c in self.statements("INSERT INTO ai.llm_calls")]


@pytest.fixture
def app(monkeypatch):
    keys = ("redis", "memory_caps", "memory_cost", "provider", "http", "db_pool")
    saved = {k: getattr(main.app.state, k, None) for k in keys}
    monkeypatch.setattr(main.settings, "llm_classifier_daily_call_cap", 40)
    monkeypatch.setattr(main.settings, "llm_model_classifier", MODEL)
    monkeypatch.setattr(main.settings, "llm_model", MODEL)
    monkeypatch.setattr(main.settings, "llm_verdict_cache", False)

    def make(world=None, provider=None, pool=None, redis="fake"):
        world = world or World()
        state = main.app.state
        state.provider = provider if provider is not None else Provider()
        state.redis = FakeRedis() if redis == "fake" else redis
        state.db_pool = None if pool is False else (pool or Store())
        state.memory_caps = {cache.STATE_LLM_CALLS: cache.MemoryCap(),
                             cache.STATE_CLASSIFIER_CALLS: cache.MemoryCap()}
        state.memory_cost = cache.MemoryCost()
        state.http = httpx.AsyncClient(transport=httpx.MockTransport(world))
        return TestClient(main.app), world, state

    yield make
    for k, v in saved.items():
        setattr(main.app.state, k, v)


def post(client, path="/analyze/AAPL", **params):
    return client.post(path, params=params)


# ── The happy path ───────────────────────────────────────────────

def test_analyze_returns_stores_and_ledgers_a_verdict(app):
    client, world, state = app()
    resp = post(client, "/analyze/aapl")
    assert resp.status_code == 200, resp.text
    out = resp.json()

    assert (out["ticker"], out["cached"], out["stored"], out["verdictId"]) == ("AAPL", False, True, VERDICT_ID)
    assert (out["entry"], out["entrySource"]) == (50.0, "last_close")
    plan = out["verdict"]["plan"]
    assert (plan["stop"], plan["disasterLine"], plan["sizeShares"]) == (46.6, 45.4, 73)
    assert plan["targets"] == [{"price": 56.9, "r": 2.03, "basis": "T1 56.90: resistance 56.90-57.30"}]
    assert plan["overhead"] == [{"price": 53.9, "r": 1.15, "basis": "overhead 53.90: resistance 53.90-54.30"}]
    assert plan["lossAtDisasterPct"] == 1.34
    assert plan["earningsInDays"] is not None and plan["invalidation"] == GO["invalidation"]
    assert out["regime"] == "CAUTIOUS" and out["macroStatus"] == "ok" and out["planRejection"] is None

    row = state.db_pool.row
    assert row["entry"] == Decimal("50.00") and row["entry_source"] == "last_close"
    assert json.loads(row["dossier"])["ticker"] == "AAPL", "the full dossier snapshot"
    assert json.loads(row["prompt_inputs"])["plan"]["stop"] == 46.6
    assert json.loads(row["prompt_inputs"])["planMathVersion"] == 2 == row["plan_math_version"]
    assert json.loads(row["plan_proposed"])["overhead"][0]["price"] == 53.9
    assert row["macro_brief_id"] is None and row["regime"] == "CAUTIOUS"
    assert state.db_pool.tx_open == 1

    (call,) = state.db_pool.ledger()
    assert (call["route"], call["label"], call["outcome"], call["ticker"]) == ("analyze", "verdict", "ok", "AAPL")
    assert call["counters"] == ["llm_calls"] and call["verdict_id"] == VERDICT_ID
    assert call["host"] == "Anthropic", "the host that served the verdict is on its ledger row"
    assert (call["tokens_in"], call["cache_write_tokens"], float(call["cost_usd"])) == (6100, 1400, 0.0212)

    sent = state.provider.calls[0]
    assert sent["label"] == "verdict" and sent["model"] == MODEL and sent["cache_system"] is False
    assert "25000" not in sent["user"], "the account size never reaches the model"
    assert ("GET", "data-engine-dev/dossier/AAPL") in world.requests or \
        any(r[1].endswith("/dossier/AAPL") for r in world.requests)


def test_entry_given_is_stored_and_keyed_in_cents(app):
    client, _, state = app()
    out = post(client, entry=50.004).json()
    assert (out["entry"], out["entrySource"]) == (50.0, "given")
    assert state.db_pool.row["entry_source"] == "given"
    assert any(k.endswith(":AAPL:swing:5000") for k in state.redis.store if "verdict:" in k)


def test_cache_system_follows_the_setting(app, monkeypatch):
    monkeypatch.setattr(main.settings, "llm_verdict_cache", True)
    client, _, state = app()
    assert post(client).status_code == 200
    assert state.provider.calls[0]["cache_system"] is True


# ── Refusals before anything is spent ────────────────────────────

@pytest.mark.parametrize("path, params", [
    ("/analyze/TOOLONG", {}), ("/analyze/BRK.B", {}), ("/analyze/A1", {}),
    ("/analyze/AAPL", {"horizon": "day"}), ("/analyze/AAPL", {"entry": 0}),
    ("/analyze/AAPL", {"entry": -5}), ("/analyze/AAPL", {"entry": "nan"}),
    ("/analyze/AAPL", {"entry": "inf"}), ("/analyze/AAPL", {"entry": "abc"}),
])
def test_bad_input_is_422_before_any_call(app, path, params):
    client, world, state = app()
    assert post(client, path, **params).status_code == 422
    assert world.requests == [] and state.provider.calls == [] and state.db_pool.calls == []


def test_no_pool_is_503_and_spends_nothing(app):
    client, world, state = app(pool=False)
    resp = post(client)
    assert resp.status_code == 503 and resp.json()["detail"] == "database unavailable"
    assert world.requests == [] and state.provider.calls == []


@pytest.mark.parametrize("pool", [Store(settings_row=False), Store(account=None)])
def test_missing_settings_is_409(app, pool):
    client, world, state = app(pool=pool)
    resp = post(client)
    assert resp.status_code == 409 and "docs/runbook.md" in resp.json()["detail"]
    assert world.requests == [] and state.provider.calls == []


def test_settings_db_error_is_503(app):
    client, world, state = app(pool=Store(raise_on="users.settings"))
    assert post(client).status_code == 503
    assert world.requests == [] and state.provider.calls == []


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(500),
                                      httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_dossier_unavailable_is_503(app, response):
    world = World()
    world.dossier_response = response
    client, _, state = app(world=world)
    assert post(client).status_code == 503
    assert state.provider.calls == [] and state.db_pool.ledger() == []


def test_dossier_404_passes_through(app):
    world = World()
    world.dossier_response = httpx.Response(404, json={"detail": "no bars"})
    client, _, state = app(world=world)
    assert post(client).status_code == 404 and state.provider.calls == []


def test_dossier_without_indicators_is_502(app):
    world = World()
    world.dossier = dossier(status="error")
    client, _, state = app(world=world)
    assert post(client).status_code == 502 and state.provider.calls == []


def test_malformed_zone_is_502_not_a_crash(app):
    world = World()
    world.dossier = dossier(zones=[{"low": 48.0, "high": 47.0}] + ZONES[1:])
    client, _, state = app(world=world)
    assert post(client).status_code == 502 and state.provider.calls == []


# ── Macro: fail-open ─────────────────────────────────────────────

@pytest.mark.parametrize("health", [httpx.Response(500), httpx.ConnectError("down")])
def test_risk_shield_down_still_analyzes(app, health):
    world = World()
    world.health = health
    client, _, state = app(world=world)
    out = post(client).json()
    assert out["macroStatus"] == "unavailable" and out["regime"] is None and out["stored"] is True
    assert json.loads(state.db_pool.row["prompt_inputs"])["macro"]["status"] == "unavailable"


def test_no_macro_brief_is_not_an_error(app):
    client, _, state = app()
    assert post(client).status_code == 200
    assert state.db_pool.row["macro_brief_id"] is None


def test_macro_brief_id_is_stored_when_one_exists(app):
    world = World()
    brief_id = "7d0c0000-0000-4000-8000-000000000001"
    world.brief = httpx.Response(200, json={"id": brief_id, "generatedAt": "x", "ageMinutes": 5,
                                            "regime": "CAUTIOUS", "brief": {"regimeView": "v"}})
    client, _, state = app(world=world)
    assert post(client).status_code == 200
    assert state.db_pool.row["macro_brief_id"] == brief_id
    assert json.loads(state.db_pool.row["prompt_inputs"])["macro"]["brief"]["brief"] == {"regimeView": "v"}


# ── News ─────────────────────────────────────────────────────────

def test_no_news_makes_no_classifier_call(app):
    client, world, state = app()
    out = post(client).json()
    assert state.provider.count("headline_classify") == 0 and out["classifier"]["calls"] == 0
    assert out["newsClassified"] is True and world.writes == []


def test_labelled_headlines_are_never_resent(app):
    world = World()
    world.dossier = dossier(news=[news_item(0, LABEL), news_item(1, LABEL), news_item(2), news_item(3)])
    client, _, state = app(world=world)
    out = post(client).json()

    assert state.provider.count("headline_classify") == 1
    classify = [c for c in state.provider.calls if c["label"] == "headline_classify"][0]
    assert "Headline 2" in classify["user"] and "Headline 3" in classify["user"]
    assert "Headline 0" not in classify["user"] and "Headline 1" not in classify["user"]
    assert "- aapl-guidance-cut" in classify["user"], "known keys offered for reuse"
    assert out["classifier"] == {"calls": 1, "classified": 2, "cached": 0, "writtenBack": 2,
                                 "writeBackErrors": 0, "ok": True}
    assert [w[0] for w in world.writes] == ["/news/502/sentiment", "/news/503/sentiment"]
    assert world.writes[0][1]["eventKey"] == "aapl-story-0"

    rows = state.db_pool.ledger()
    # Stamped at write time: the classifier's row sorts before its verdict's
    # (the first live call had them backwards, the verdict at request start).
    assert rows[0]["called_at"] < rows[1]["called_at"]
    assert rows == sorted(rows, key=lambda r: r["called_at"])
    assert [(r["label"], r["route"], r["counters"]) for r in rows] == [
        ("headline_classify", "analyze", ["llm_calls", "classifier_calls"]),
        ("verdict", "analyze", ["llm_calls"])]
    assert rows[0]["ticker"] == "AAPL"


def test_every_headline_labelled_means_no_classifier_call(app):
    world = World()
    world.dossier = dossier(news=[news_item(0, LABEL), news_item(1, LABEL)])
    client, _, state = app(world=world)
    assert post(client).status_code == 200
    assert state.provider.count("headline_classify") == 0 and world.writes == []


def test_same_event_key_groups_to_one_line(app):
    world = World()
    world.dossier = dossier(news=[news_item(i, LABEL) for i in range(3)])
    client, _, state = app(world=world)
    assert post(client).status_code == 200
    (event,) = json.loads(state.db_pool.row["prompt_inputs"])["events"]
    assert event["eventKey"] == "aapl-guidance-cut" and event["sources"] == 3


def test_item_without_id_skips_writeback(app):
    world = World()
    world.dossier = dossier(news=[news_item(0, with_id=False), news_item(1)])
    client, _, _ = app(world=world)
    out = post(client).json()
    assert out["classifier"]["classified"] == 2 and out["classifier"]["writtenBack"] == 1
    assert [w[0] for w in world.writes] == ["/news/501/sentiment"]


@pytest.mark.parametrize("error", [LLMCapExceeded("cap"), LLMCooledDown("cd"), LLMRateLimited("429"),
                                   LLMUnavailable("down"), LLMBadResponse("bad")])
def test_classifier_refusal_fails_open(app, error):
    world = World()
    world.dossier = dossier(news=[news_item(0), news_item(1)])
    client, _, state = app(world=world, provider=Provider(classify_raises=error))
    out = post(client).json()

    assert out["newsClassified"] is False and out["stored"] is True
    assert state.provider.count("verdict") == 1
    inputs = json.loads(state.db_pool.row["prompt_inputs"])
    assert inputs["dataQuality"]["newsClassifier"] == "unavailable"
    assert [e["classified"] for e in inputs["events"]] == [False, False]
    assert world.writes == []


# ── Plan rejection ───────────────────────────────────────────────

def test_ath_no_target_passes_through_as_wait(app):
    """The open ATH gap: no zone above the entry, no synthetic target."""
    world = World()
    world.dossier = dossier(zones=ZONES[:2] + [], close=60.0)
    client, _, state = app(world=world, provider=Provider(verdict=WAIT))
    out = post(client).json()

    assert out["verdict"]["verdict"] == "wait" and out["verdict"]["plan"] is None
    assert out["planRejection"]["reason"] == "no_target"
    assert out["verdict"]["riskFlags"][0] == "no plan: no_target"
    schema = state.provider.calls[0]["schema"]
    assert schema["properties"]["verdict"]["enum"] == ["wait", "avoid"]
    assert json.loads(state.db_pool.row["plan_rejection"])["reason"] == "no_target"
    assert state.db_pool.row["plan_proposed"] is None


@pytest.mark.parametrize("answer", [
    dict(GO),                                               # `go` although the plan was rejected
    {**WAIT, "thesis": ["only", "two"]},
    {**WAIT, "confidence": 140},
])
def test_bad_verdict_answer_is_502_and_stores_nothing(app, answer):
    world = World()
    world.dossier = dossier(zones=ZONES[:2], close=60.0)
    client, _, state = app(world=world, provider=Provider(verdict=answer))
    resp = post(client)

    assert resp.status_code == 502 and "VerdictRejected" in resp.json()["detail"]
    assert state.db_pool.row is None
    (row,) = state.db_pool.ledger()
    assert (row["outcome"], row["tokens_in"], row["verdict_id"]) == ("bad_response", 6100, None)
    assert not any("verdict:" in k or "lock:" in k for k in state.redis.store)
    assert float(state.redis.store[f"tf:ai:state:cost_day:{cache.et_day()}"]) == pytest.approx(0.0212)


# ── LLM failures ─────────────────────────────────────────────────

@pytest.mark.parametrize("error, status", [
    (LLMCapExceeded("cap"), 429), (LLMCooledDown("cd"), 429), (LLMNotConfigured("no key"), 503)])
def test_pre_wire_refusal_writes_no_ledger_row(app, error, status):
    client, _, state = app(provider=Provider(raises=error))
    assert post(client).status_code == status
    assert state.db_pool.ledger() == [] and state.db_pool.row is None
    assert not any("lock:" in k for k in state.redis.store), "the lock is released"


@pytest.mark.parametrize("error, status, outcome", [
    (LLMRateLimited("429"), 429, "rate_limited"), (LLMUnavailable("down"), 503, "unavailable"),
    (LLMRefused("no"), 502, "refused"), (LLMBadResponse("cut"), 502, "bad_response")])
def test_post_wire_failure_writes_ledger_row(app, error, status, outcome):
    client, _, state = app(provider=Provider(raises=error))
    assert post(client).status_code == status
    (row,) = state.db_pool.ledger()
    assert (row["outcome"], row["label"], row["ticker"], row["cost_usd"]) == (outcome, "verdict", "AAPL", None)
    assert state.db_pool.row is None and not any("lock:" in k for k in state.redis.store)


def test_a_request_this_service_built_wrong_is_500(app):
    client, _, state = app(provider=Provider(raises=ValueError("label must match")))
    resp = post(client)
    assert resp.status_code == 500 and state.db_pool.ledger() == []
    assert not any("lock:" in k for k in state.redis.store)


def test_analyze_passes_swing_low_and_ema20_when_present(app, monkeypatch):
    """The dossier's ema20 reaches plan math, and so does `lastSwingLow`
    once data-engine sends it (4.8a-de); until then both swing kwargs are
    None and plan math needs no edit when the field arrives."""
    import analyst
    seen = []
    real = analyst.compute_plan

    def spy(**kw):
        seen.append(kw)
        return real(**kw)

    monkeypatch.setattr(analyst, "compute_plan", spy)
    client, world, _ = app()
    assert post(client).status_code == 200
    assert seen[-1]["ema20"] == 47.5 and seen[-1]["swing_low"] is None and seen[-1]["swing_low_date"] is None
    assert [z["side"] for z in seen[-1]["zones"]] == ["support", "support", "resistance", "resistance"]
    assert [z["low"] for z in seen[-1]["zones"]] == [z["low"] for z in ZONES], "data-engine's order, its label"
    world.dossier = dossier()
    world.dossier["sections"]["indicators"]["lastSwingLow"] = {"price": 48.5, "date": "2026-09-15"}
    assert post(client, fresh="true").status_code == 200
    assert (seen[-1]["swing_low"], seen[-1]["swing_low_date"]) == (48.5, "2026-09-15")
    # a malformed field is ignored, not a 502
    world.dossier["sections"]["indicators"]["lastSwingLow"] = "48.5"
    assert post(client, fresh="true").status_code == 200
    assert seen[-1]["swing_low"] is None
