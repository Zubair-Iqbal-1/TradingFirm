"""
TradingFirm — /analyze's two upstream reads (Part 4.4).

data-engine's dossier is the verdict's subject, so it fails CLOSED: without
it nothing is spent. risk-shield's regime and macro brief are context, so
they fail OPEN: the verdict runs and is told the macro view is unavailable.

Both use the lifespan's httpx client; tests use httpx.MockTransport, never
respx (spec 4.2 decision 15). Messages carry a status or an exception type,
never a body or a URL.
"""

import logging
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
