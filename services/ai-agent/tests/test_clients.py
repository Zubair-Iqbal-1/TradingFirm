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
