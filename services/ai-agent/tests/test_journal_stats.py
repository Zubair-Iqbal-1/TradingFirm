"""Part 4.5 — GET /journal/stats and journal/stats.py. The route against a
fake pool; the numbers from hand-made rows. Nothing opens a socket."""

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import db
import main
from journal import stats
from tests.fake_pool import FakePool

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 7, 18, 0, tzinfo=ET).astimezone(timezone.utc)
ASKED = datetime(2026, 9, 21, 17, 0, tzinfo=ET).astimezone(timezone.utc)   # day 0 = 09-21
SONNET, GLM = "anthropic/claude-sonnet-5", "z-ai/glm-5"


def row(n, verdict="go", h=1, ret="1.000", model=SONNET, confidence=70, plan=True,
        stop=None, target=None, first=None, r=None, asked=ASKED):
    return {"id": f"{n:08d}-0000-4000-8000-000000000000", "model": model, "verdict": verdict,
            "confidence": confidence, "asked_at": asked, "has_plan": plan,
            "horizon_days": h, "return_pct": None if ret is None else Decimal(ret),
            "stop_hit": stop, "target_hit": target, "first_hit": first,
            "r_multiple": None if r is None else Decimal(r),
            "session_date": date(2026, 9, 22) if h == 1 else date(2026, 9, 28) if h == 5 else None}


def unscored(n, **kw):
    out = row(n, **kw)
    out.update(horizon_days=None, return_pct=None, session_date=None)
    return out


def group(answer, verdict, h, model=SONNET):
    [m] = [x for x in answer["models"] if x["model"] == model]
    return m["byVerdict"][verdict][str(h)]


# ── The numbers ──────────────────────────────────────────────────

def test_hit_rate_by_verdict():
    rows = [row(1, "go", ret="2.000"), row(2, "go", ret="-1.000"), row(3, "go", ret="0.000"),
            row(4, "avoid", ret="-1.000"), row(5, "avoid", ret="0.500"),
            row(6, "wait", ret="3.000")]
    got = stats.compute(rows, NOW, 90)
    go = group(got, "go", 1)
    assert go["hitRate"] == 0.333, "0 is a miss"
    assert go["meanReturnPct"] == 0.333 and go["medianReturnPct"] == 0.0
    assert group(got, "avoid", 1)["hitRate"] == 0.5
    wait = group(got, "wait", 1)
    assert wait["hitRate"] is None and wait["meanReturnPct"] == 3.0


def test_pending_and_expired_counted_apart():
    """Asked 09-21 after the close; NOW = 10-07. +1 (09-22) is 11 sessions
    old → expired; +5 (09-28) is 7 → pending; +20 (10-19) not due → pending."""
    got = stats.compute([unscored(1)], NOW, 90)
    assert [group(got, "go", h)["expired"] for h in (1, 5, 20)] == [1, 0, 0]
    assert [group(got, "go", h)["pending"] for h in (1, 5, 20)] == [0, 1, 1]
    assert all(group(got, "go", h)["scored"] == 0 and group(got, "go", h)["asked"] == 1
               for h in (1, 5, 20))
    one = group(got, "go", 1)
    assert one["hitRate"] is None and one["meanReturnPct"] is None, "null, never 0"


def test_avg_r_over_plans_only():
    rows = [row(1, ret="2.000", stop=False, target=True, first="target", r="1.000"),
            row(2, ret="-3.000", stop=True, target=False, first="stop", r="-1.000"),
            row(3, ret="5.000", plan=False)]
    g = group(stats.compute(rows, NOW, 90), "go", 1)
    assert g["withPlan"] == 2 and g["avgR"] == 0.0
    assert g["stopHitRate"] == 0.5 and g["targetHitRate"] == 0.5 and g["targetFirstRate"] == 0.5
    assert g["scored"] == 3 and g["meanReturnPct"] == 1.333


def test_stats_never_mix_models():
    """The same ticker, two models: two groups, each rate only its own."""
    rows = [row(1, ret="2.000", model=SONNET, confidence=80),
            row(2, ret="-2.000", model=GLM, confidence=80),
            row(3, ret="4.000", model=SONNET, confidence=85)]
    got = stats.compute(rows, NOW, 90)
    assert [m["model"] for m in got["models"]] == [SONNET, GLM], "one entry each, sorted"
    assert group(got, "go", 1, SONNET)["hitRate"] == 1.0
    assert group(got, "go", 1, GLM)["hitRate"] == 0.0
    assert group(got, "go", 1, SONNET)["meanReturnPct"] == 3.0
    assert group(got, "go", 1, GLM)["asked"] == 1
    sonnet_cal = next(m for m in got["models"] if m["model"] == SONNET)["calibration"]["1"]
    glm_cal = next(m for m in got["models"] if m["model"] == GLM)["calibration"]["1"]
    assert [b["n"] for b in sonnet_cal if b["bucket"] == "80-89"] == [2]
    assert [b["n"] for b in glm_cal if b["bucket"] == "80-89"] == [1]


def test_calibration_bucket_edges():
    rows = [row(i, verdict="go", ret="1.000", confidence=c) for i, c in
            enumerate((49, 50, 89, 90, 100), start=1)]
    rows.append(row(9, verdict="wait", ret="1.000", confidence=55))        # waits never count
    cal = stats.compute(rows, NOW, 90)["models"][0]["calibration"]["1"]
    assert {b["bucket"]: b["n"] for b in cal} == {
        "0-49": 1, "50-59": 1, "60-69": 0, "70-79": 0, "80-89": 1, "90-100": 2}
    top = next(b for b in cal if b["bucket"] == "90-100")
    assert top["meanConfidence"] == 95.0 and top["hitRate"] == 1.0
    assert next(b for b in cal if b["bucket"] == "60-69")["hitRate"] is None


def test_scored_through_and_fold():
    rows = [row(1, h=1), row(1, h=5)]
    got = stats.compute(rows, NOW, 30)
    assert got["scoredThrough"] == "2026-09-28" and got["days"] == 30
    assert group(got, "go", 1)["asked"] == 1 == group(got, "go", 5)["asked"]


# ── The route ────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    async def boom(*a, **kw):
        raise ConnectionRefusedError("postgres is down")

    async def no_redis(*a, **kw):
        raise ConnectionRefusedError("redis is down")

    import cache
    monkeypatch.setattr(db, "create_db_pool", boom)
    monkeypatch.setattr(cache, "create_redis", no_redis)
    monkeypatch.setattr(main.settings, "journal_scoring_enabled", False)
    with TestClient(main.app) as c:
        yield c


@pytest.mark.parametrize("days", ["0", "366", "-1", "abc", "1.5"])
def test_stats_bad_days_is_422(client, days):
    main.app.state.db_pool = FakePool()
    assert client.get(f"/journal/stats?days={days}").status_code == 422


def test_stats_without_pool_is_503(client):
    main.app.state.db_pool = None
    r = client.get("/journal/stats")
    assert r.status_code == 503


def test_stats_db_error_is_503(client):
    main.app.state.db_pool = FakePool(raise_on="FROM ai.verdicts v")
    assert client.get("/journal/stats").status_code == 503


def test_stats_empty_window(client):
    pool = FakePool({"FROM ai.verdicts v": []})
    main.app.state.db_pool = pool
    r = client.get("/journal/stats?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == [] and body["days"] == 7 and body["scoredThrough"] is None
    (_, _, args), = pool.calls
    assert args[0] == db.DEV_USER_ID
    assert (datetime.fromisoformat(body["asOf"]) - args[1]).days == 7


def test_stats_is_read_only(client):
    pool = FakePool({"FROM ai.verdicts v": [row(1), row(2, verdict="avoid", ret="-1.000")]})
    main.app.state.db_pool = pool
    first = client.get("/journal/stats").json()
    second = client.get("/journal/stats").json()
    first.pop("asOf"), second.pop("asOf")
    assert first == second
    assert len(pool.calls) == 2 and all(c[0] == "fetch" for c in pool.calls)
    assert not any(w in c[1] for c in pool.calls for w in ("INSERT", "UPDATE", "DELETE"))
    assert first["models"][0]["byVerdict"]["avoid"]["1"]["hitRate"] == 1.0
