"""Part 4.4 — /analyze's upstream reads. httpx.MockTransport, never respx
(spec 4.2 decision 15). The dossier fails closed, risk-shield fails open."""

import httpx
import pytest

import upstream

DE = "http://data-engine-dev:8001"
RS = "http://risk-shield-dev:8003"


def dossier(ticker="AAPL", status="ok", close=50.0):
    return {"ticker": ticker, "horizon": "swing", "asOf": "2026-09-18T00:00:00Z",
            "sections": {"indicators": {"status": status, "close": close, "atr14": 1.2,
                                        "zones": {"support": [], "resistance": []}}}}


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fetch_dossier_request_and_answer():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=dossier())

    async with client(handler) as http:
        got = await upstream.fetch_dossier(http, DE + "/", " aapl ", "swing", 5)
    assert got["ticker"] == "AAPL"
    assert seen == [f"{DE}/dossier/AAPL?horizon=swing"], "normalized before it reaches a URL"


@pytest.mark.asyncio
@pytest.mark.parametrize("response, error", [
    (httpx.Response(404, json={"detail": "no bars"}), upstream.DossierNotFound),
    (httpx.Response(503), upstream.DossierUnavailable),
    (httpx.Response(500), upstream.DossierUnavailable),
    (httpx.Response(429), upstream.DossierUnavailable),
    (httpx.Response(200, text="<html>"), upstream.DossierInvalid),
    (httpx.Response(200, json=[1]), upstream.DossierInvalid),
    (httpx.Response(200, json=dossier(ticker="MSFT")), upstream.DossierInvalid),
    (httpx.Response(200, json=dossier(status="error")), upstream.DossierInvalid),
    (httpx.Response(200, json=dossier(close=None)), upstream.DossierInvalid),
    (httpx.Response(200, json=dossier(close=0)), upstream.DossierInvalid),
])
async def test_fetch_dossier_failures_are_typed(response, error):
    async with client(lambda r: response) as http:
        with pytest.raises(error) as info:
            await upstream.fetch_dossier(http, DE, "AAPL", "swing", 5)
    assert "http://" not in str(info.value), "no URL in a message"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x")])
async def test_fetch_dossier_transport_error_is_unavailable(exc):
    def handler(request):
        raise exc
    async with client(handler) as http:
        with pytest.raises(upstream.DossierUnavailable) as info:
            await upstream.fetch_dossier(http, DE, "AAPL", "swing", 5)
    assert info.value.__cause__ is None and type(exc).__name__ in str(info.value)


@pytest.mark.asyncio
async def test_bad_ticker_or_horizon_never_reaches_http():
    def handler(request):
        raise AssertionError("no request expected")
    async with client(handler) as http:
        with pytest.raises(ValueError):
            await upstream.fetch_dossier(http, DE, "AAPL/../x", "swing", 5)
        with pytest.raises(ValueError):
            await upstream.fetch_dossier(http, DE, "AAPL", "day", 5)


HEALTH = {"score": 66, "regime": "CAUTIOUS", "trend": "stable", "stale": False,
          "checkedAt": "2026-09-21T16:55:00+00:00",
          "overlay": {"cap": None, "base": 66, "esPct": 1.2, "nqPct": 2.4, "capped": False,
                      "status": "within", "movePct": 1.2},
          "weekend": None, "newsPollStale": False}


@pytest.mark.asyncio
async def test_no_macro_brief_is_not_an_error(caplog):
    """Today's state: risk-shield is live, 4.6 is not, so /macro/brief 404s."""
    def handler(request):
        if request.url.path == "/market/health":
            return httpx.Response(200, json=HEALTH)
        return httpx.Response(404, json={"detail": "no macro brief yet"})

    with caplog.at_level("WARNING"):
        async with client(handler) as http:
            macro = await upstream.fetch_macro(http, RS, 5)
    assert macro["status"] == "ok" and macro["regime"] == "CAUTIOUS" and macro["score"] == 66
    assert macro["overlay"] == {"capped": False, "cap": None, "movePct": 1.2, "status": "within"}
    assert macro["brief"] is None and macro["briefId"] is None
    assert caplog.text == "", "a 404 brief is the expected state, not a warning"


@pytest.mark.asyncio
async def test_macro_brief_is_carried_when_one_exists():
    brief = {"id": "7d0c0000-0000-4000-8000-000000000001", "generatedAt": "2026-09-21T11:30:00+00:00",
             "ageMinutes": 300, "regime": "CAUTIOUS", "brief": {"regimeView": "v", "keyRisks": ["k"]},
             "briefText": "long text", "freshness": {}}

    def handler(request):
        return httpx.Response(200, json=HEALTH if request.url.path == "/market/health" else brief)

    async with client(handler) as http:
        macro = await upstream.fetch_macro(http, RS, 5)
    assert macro["briefId"] == brief["id"]
    assert macro["brief"]["brief"] == {"regimeView": "v", "keyRisks": ["k"]}
    assert "briefText" not in macro["brief"]


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", ["connect", "500", "html", "no-regime"])
async def test_risk_shield_down_still_answers(behaviour):
    def handler(request):
        if behaviour == "connect":
            raise httpx.ConnectError("down")
        if behaviour == "500":
            return httpx.Response(500)
        if behaviour == "html":
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json={"score": 1})

    async with client(handler) as http:
        macro = await upstream.fetch_macro(http, RS, 5)
    assert macro["status"] == "unavailable" and macro["regime"] is None and macro["brief"] is None


# ── Part 4.5: the journal's refresh and bars reads ───────────────

def refresh_body(daily=502, hourly=455):
    return {"ticker": "AAPL", "dailyBars": daily, "hourlyBars": hourly,
            "earningsDates": {"source": "yfinance", "stored": 8, "dropped": 0, "reason": None}}


@pytest.mark.asyncio
async def test_refresh_ok_request_shape():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json=refresh_body())

    async with client(handler) as http:
        got = await upstream.refresh_ticker(http, DE + "/", " aapl ", 120)
    assert got.kind == upstream.REFRESH_OK and (got.daily, got.hourly) == (502, 455)
    assert seen == [("POST", f"{DE}/stock/AAPL/refresh")]


@pytest.mark.asyncio
@pytest.mark.parametrize("response, kind", [
    (httpx.Response(429, headers={"Retry-After": "412"}, json={"detail": "recently"}), "cooldown"),
    (httpx.Response(429, json={"detail": "Rate limited by data provider."}), "stop"),
    (httpx.Response(502), "stop"),
    (httpx.Response(503), "stop"),
    (httpx.Response(400), "stop"),
    (httpx.Response(200, text="<html>"), "stop"),
    (httpx.Response(200, json={"ticker": "AAPL"}), "stop"),
    (httpx.Response(200, json=refresh_body(daily=True)), "stop"),
    (httpx.Response(200, json=refresh_body(daily=0, hourly=0)), "blank"),   # F2
    (httpx.Response(200, json=refresh_body(daily=502, hourly=0)), "blank"),
    (httpx.Response(200, json=refresh_body(daily=0, hourly=455)), "blank"),
], ids=["cooldown", "provider-429", "502", "503", "400", "not-json", "no-counts",
        "bool-count", "blank-both", "blank-hourly", "blank-daily"])
async def test_refresh_answers_are_sorted(response, kind):
    async with client(lambda request: response) as http:
        got = await upstream.refresh_ticker(http, DE, "AAPL", 120)
    assert got.kind == kind, got


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_refresh_transport_error_is_stop(exc):
    def handler(request):
        raise exc

    async with client(handler) as http:
        got = await upstream.refresh_ticker(http, DE, "AAPL", 120)
    assert got.kind == upstream.REFRESH_STOP and type(exc).__name__ in got.detail


def bars_body(interval="1d", bars=None):
    return {"ticker": "AAPL", "interval": interval, "bars": bars if bars is not None else [
        {"ts": "2026-09-18T00:00:00+00:00", "open": 337.91, "high": 338.49,
         "low": 332.53, "close": 336.13, "volume": 86433100}]}


@pytest.mark.asyncio
async def test_fetch_bars_request_and_answer():
    from datetime import datetime, timezone
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=bars_body())

    since = datetime(2026, 9, 18, tzinfo=timezone.utc)
    async with client(handler) as http:
        got = await upstream.fetch_bars(http, DE, "aapl", "1d", since, 10)
    assert got[0]["ts"] == "2026-09-18T00:00:00+00:00"
    assert seen[0].path == "/stock/AAPL/bars"
    assert dict(seen[0].params) == {"interval": "1d", "since": "2026-09-18T00:00:00+00:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"detail": "No stored bars"}),
    httpx.Response(503),
    httpx.Response(200, text="nope"),
    httpx.Response(200, json=bars_body(interval="1h")),
    httpx.Response(200, json=bars_body(bars=[{"ts": "2026-09-18T00:00:00+00:00", "open": 1}])),
    httpx.Response(200, json=bars_body(bars=[{"ts": 5, "open": 1, "high": 1, "low": 1, "close": 1}])),
], ids=["404", "503", "not-json", "wrong-interval", "missing-keys", "ts-not-str"])
async def test_bars_read_failures_are_typed(response):
    from datetime import datetime, timezone
    async with client(lambda request: response) as http:
        with pytest.raises(upstream.BarsUnavailable):
            await upstream.fetch_bars(http, DE, "AAPL", "1d", datetime(2026, 9, 18, tzinfo=timezone.utc), 10)


@pytest.mark.asyncio
async def test_journal_reads_refuse_bad_input_before_http():
    from datetime import datetime, timezone

    def handler(request):
        raise AssertionError("no request may be sent")

    async with client(handler) as http:
        with pytest.raises(ValueError):
            await upstream.refresh_ticker(http, DE, "AAPL1", 120)
        with pytest.raises(ValueError):
            await upstream.fetch_bars(http, DE, "AAPL", "5m", datetime(2026, 9, 18, tzinfo=timezone.utc), 10)
        with pytest.raises(ValueError):
            await upstream.fetch_bars(http, DE, "AAPL", "1d", datetime(2026, 9, 18), 10)
