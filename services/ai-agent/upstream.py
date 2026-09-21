"""
TradingFirm — ai-agent's upstream reads: /analyze's two (Part 4.4) and the
journal scorer's refresh + bars reads on data-engine (Part 4.5, at the end).

data-engine's dossier is the verdict's subject, so it fails CLOSED: without
it nothing is spent. risk-shield's regime and macro brief are context, so
they fail OPEN: the verdict runs and is told the macro view is unavailable.

Both use the lifespan's httpx client; tests use httpx.MockTransport, never
respx (spec 4.2 decision 15). Messages carry a status or an exception type,
never a body or a URL.
"""

import logging
from datetime import datetime
from typing import Optional

import httpx

from tickers import validate_ticker

logger = logging.getLogger(__name__)

HORIZONS = ("swing",)

MACRO_OK = "ok"
MACRO_UNAVAILABLE = "unavailable"


class DossierError(Exception):
    """Base. The route maps each subclass to a status."""


class DossierNotFound(DossierError):
    """data-engine answered 404: no bars stored for the ticker."""


class DossierUnavailable(DossierError):
    """data-engine is down, timed out or answered 5xx / anything unexpected."""


class DossierInvalid(DossierError):
    """A 200 that is not a usable dossier: wrong shape, wrong ticker, or an
    indicators section that is not `ok` (no close, no ATR, no zones)."""


async def fetch_dossier(http: httpx.AsyncClient, base_url: str, ticker: str,
                        horizon: str, timeout: float) -> dict:
    ticker = validate_ticker(ticker)
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")
    url = f"{base_url.rstrip('/')}/dossier/{ticker}"
    try:
        resp = await http.get(url, params={"horizon": horizon}, timeout=timeout)
    except httpx.HTTPError as e:
        raise DossierUnavailable(f"data-engine: {type(e).__name__}") from None
    if resp.status_code == 404:
        raise DossierNotFound(f"no bars stored for {ticker}")
    if resp.status_code != 200:
        raise DossierUnavailable(f"data-engine: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise DossierInvalid("data-engine: body is not JSON") from None

    sections = body.get("sections") if isinstance(body, dict) else None
    if not isinstance(sections, dict) or body.get("ticker") != ticker:
        raise DossierInvalid("data-engine: not a dossier for this ticker")
    indicators = sections.get("indicators")
    if not isinstance(indicators, dict) or indicators.get("status") != "ok":
        raise DossierInvalid("dossier indicators section is not ok")
    close = indicators.get("close")
    if isinstance(close, bool) or not isinstance(close, (int, float)) or not close > 0:
        raise DossierInvalid("dossier has no usable close")
    return body


async def _get_json(http, url: str, timeout: float) -> tuple[Optional[int], Optional[dict]]:
    try:
        resp = await http.get(url, timeout=timeout)
    except httpx.HTTPError as e:
        logger.warning(f"risk-shield read failed: {type(e).__name__}")
        return None, None
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body if isinstance(body, dict) else None


async def fetch_macro(http: httpx.AsyncClient, base_url: str, timeout: float) -> dict:
    """The regime and, when one exists, the latest macro brief. Never raises.

    Until Part 4.6 ships and MACRO_BRIEF_ENABLED goes on, GET /macro/brief
    answers 404 `no macro brief yet`: that is `brief: None`, not an error.
    """
    base = base_url.rstrip("/")
    out = {"status": MACRO_UNAVAILABLE, "regime": None, "score": None, "trend": None,
           "stale": None, "checkedAt": None, "overlay": None, "weekend": None,
           "briefId": None, "brief": None}

    status, health = await _get_json(http, f"{base}/market/health", timeout)
    if status == 200 and health is not None and isinstance(health.get("regime"), str):
        overlay = health.get("overlay") if isinstance(health.get("overlay"), dict) else None
        weekend = health.get("weekend") if isinstance(health.get("weekend"), dict) else None
        out.update(
            status=MACRO_OK, regime=health["regime"], score=health.get("score"),
            trend=health.get("trend"), stale=health.get("stale"),
            checkedAt=health.get("checkedAt"),
            overlay={k: overlay.get(k) for k in ("capped", "cap", "movePct", "status")} if overlay else None,
            weekend={k: weekend.get(k) for k in ("level", "reasons")} if weekend else None,
        )
    elif status is not None:
        logger.warning(f"risk-shield /market/health: HTTP {status}")

    status, brief = await _get_json(http, f"{base}/macro/brief", timeout)
    if status == 200 and brief is not None and isinstance(brief.get("id"), str):
        out["briefId"] = brief["id"]
        out["brief"] = {
            "generatedAt": brief.get("generatedAt"),
            "ageMinutes": brief.get("ageMinutes"),
            "regime": brief.get("regime"),
            "brief": brief.get("brief") if isinstance(brief.get("brief"), dict) else None,
        }
    elif status not in (None, 404):
        logger.warning(f"risk-shield /macro/brief: HTTP {status}")
    return out


# ── The journal's reads (Part 4.5) ───────────────────────────────
#
# The scorer asks data-engine to refresh a ticker, then reads the stored bars
# back. The refresh answer is sorted into four kinds (spec 4.5 decisions 2
# and 11), because data-engine's statuses do not say everything:
#   ok        200 with daily AND hourly bars: written just now
#   blank     200 with zero daily or hourly bars. yfinance 1.5.1 answers a
#             delisted symbol and a rate limit the same way (F2), so a
#             blank is ambiguous; the runner's two-blank rule decides
#   cooldown  429 WITH Retry-After: data-engine's 15 min refresh cooldown.
#             It proves a completed refresh, not a stored bar (F1): never
#             fresh, requeued once
#   stop      anything else — 429 without Retry-After (the provider's rate
#             limit), 5xx, timeout, connect error, a body that is not the
#             refresh shape. The night stops.
# data-engine pins the Retry-After difference:
# test_refresh_429s_distinguishable_for_ai_agent.

REFRESH_OK, REFRESH_BLANK, REFRESH_COOLDOWN, REFRESH_STOP = "ok", "blank", "cooldown", "stop"


class RefreshAnswer:
    __slots__ = ("kind", "detail", "daily", "hourly")

    def __init__(self, kind: str, detail: str, daily: int = 0, hourly: int = 0):
        self.kind, self.detail, self.daily, self.hourly = kind, detail, daily, hourly

    def __repr__(self) -> str:
        return f"RefreshAnswer({self.kind}, {self.detail})"


def _count(body, key) -> Optional[int]:
    value = body.get(key) if isinstance(body, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def refresh_ticker(http: httpx.AsyncClient, base_url: str, ticker: str,
                         timeout: float) -> RefreshAnswer:
    """POST /stock/{t}/refresh on data-engine, sorted into a kind. Never raises
    for anything data-engine or the network does."""
    ticker = validate_ticker(ticker)
    url = f"{base_url.rstrip('/')}/stock/{ticker}/refresh"
    try:
        resp = await http.post(url, timeout=timeout)
    except httpx.HTTPError as e:
        return RefreshAnswer(REFRESH_STOP, f"data-engine: {type(e).__name__}")
    if resp.status_code == 429:
        if resp.headers.get("Retry-After"):
            return RefreshAnswer(REFRESH_COOLDOWN, "refresh cooldown")
        return RefreshAnswer(REFRESH_STOP, "data-engine: HTTP 429 (provider rate limit)")
    if resp.status_code != 200:
        return RefreshAnswer(REFRESH_STOP, f"data-engine: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        return RefreshAnswer(REFRESH_STOP, "data-engine: refresh body is not JSON")
    daily, hourly = _count(body, "dailyBars"), _count(body, "hourlyBars")
    if daily is None or hourly is None:
        return RefreshAnswer(REFRESH_STOP, "data-engine: not a refresh answer")
    if daily == 0 or hourly == 0:
        return RefreshAnswer(REFRESH_BLANK, f"blank refresh (daily {daily}, hourly {hourly})",
                             daily, hourly)
    return RefreshAnswer(REFRESH_OK, "refreshed", daily, hourly)


class BarsUnavailable(Exception):
    """A bars read that did not produce a list of bars."""


BAR_KEYS = ("ts", "open", "high", "low", "close")


async def fetch_bars(http: httpx.AsyncClient, base_url: str, ticker: str,
                     interval: str, since: datetime, timeout: float) -> list[dict]:
    """GET /stock/{t}/bars — DB-only on data-engine, never the provider.
    404 (no bars at all) and every other failure are BarsUnavailable."""
    ticker = validate_ticker(ticker)
    if interval not in ("1d", "1h"):
        raise ValueError("interval must be '1d' or '1h'")
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    url = f"{base_url.rstrip('/')}/stock/{ticker}/bars"
    try:
        resp = await http.get(url, params={"interval": interval, "since": since.isoformat()},
                              timeout=timeout)
    except httpx.HTTPError as e:
        raise BarsUnavailable(f"data-engine: {type(e).__name__}") from None
    if resp.status_code != 200:
        raise BarsUnavailable(f"data-engine: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise BarsUnavailable("data-engine: bars body is not JSON") from None
    bars = body.get("bars") if isinstance(body, dict) else None
    if not isinstance(bars, list) or body.get("ticker") != ticker or body.get("interval") != interval:
        raise BarsUnavailable("data-engine: not a bars answer for this ticker")
    for b in bars:
        if not isinstance(b, dict) or not isinstance(b.get("ts"), str) or any(
                isinstance(b.get(k), bool) or not isinstance(b.get(k), (int, float))
                for k in BAR_KEYS[1:]):
            raise BarsUnavailable("data-engine: a bar without ts / open / high / low / close")
    return bars
