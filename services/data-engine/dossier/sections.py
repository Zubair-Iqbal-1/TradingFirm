"""
TradingFirm — Section builders for the dossier (Part 2.4).

Each builder returns one section of the document. The indicator builder is
the Part 1.7 endpoint body, moved here in 2.4's `refactor:` commit so
`GET /indicators/{ticker}` and the dossier share one bars → snapshot →
cache path instead of two copies of it.

Database failures are never swallowed here: they propagate as
`db.DB_ERRORS`, which the dossier's section boundary re-raises and the
endpoint turns into a 503 (spec 2.4 decision 7).
"""

import gc
import logging
from datetime import datetime, timezone

from indicators import IndicatorsResponse, sector_etf, swing_snapshot
from indicators.models import SessionSoFarOut

logger = logging.getLogger(__name__)


class NoBarsStored(Exception):
    """The ticker has no stored daily bars. The indicators endpoint turns
    this into a 404; the dossier decides after trying a refresh."""


async def indicators_body(pool, redis, ticker: str, provider=None) -> IndicatorsResponse:
    """
    The Part 1.7 snapshot for `ticker` from stored bars, Redis-cached for
    TTL_INDICATORS. `cached` is set on the way out, never stored.

    Raises NoBarsStored when the ticker has no daily bars. Database errors
    propagate untouched (db.DB_ERRORS); the caller maps them to 503.
    """
    from bar_session import session_so_far_on_read
    from cache import get_cached_indicators, set_cached_indicators

    if redis is not None:
        try:
            cached = await get_cached_indicators(redis, ticker)
        except Exception as e:
            logger.warning(f"Indicators cache read failed for {ticker}: {e}")
            cached = None
        if cached is not None:
            try:
                return IndicatorsResponse.model_validate({
                    **cached, "cached": True,
                    "sessionSoFar": await session_so_far_on_read(redis, provider, ticker),
                })
            except Exception as e:
                logger.warning(
                    f"Indicators cache for {ticker} does not match the schema, recomputing: {e}"
                )

    from db import bars_to_df, get_bars, get_stock

    daily_rows = await get_bars(pool, ticker, "1d")
    if not daily_rows:
        raise NoBarsStored(ticker)
    spy_rows = daily_rows if ticker == "SPY" else await get_bars(pool, "SPY", "1d")
    stock = await get_stock(pool, ticker)
    sector_name = stock.get("sector") if stock else None
    etf = sector_etf(sector_name)
    if etf is None:
        etf_rows = []
    elif etf == ticker:
        etf_rows = daily_rows
    else:
        etf_rows = await get_bars(pool, etf, "1d")

    daily_df = bars_to_df(daily_rows)
    spy_df = bars_to_df(spy_rows)
    etf_df = bars_to_df(etf_rows)
    snapshot = swing_snapshot(
        daily_df,
        spy_df["Close"] if len(spy_df) else None,
        etf_df["Close"] if len(etf_df) else None,
    )
    as_of = daily_rows[-1]["ts"]
    benchmarks = {
        "spy": {"ticker": "SPY", "bars": len(spy_rows)},
        "sector": {"ticker": etf, "bars": len(etf_rows)},
    }
    del daily_df, spy_df, etf_df, daily_rows, spy_rows, etf_rows
    gc.collect()

    logger.info(
        f"Indicators {ticker}: bars={snapshot['bars']} close={snapshot['close']} "
        f"ema20={snapshot['ema20']} rsi14={snapshot['rsi14']} "
        f"spy={'yes' if snapshot['rs_spy_20'] is not None else 'no'} "
        f"sector={sector_name!r}->{etf}"
    )

    response = IndicatorsResponse(
        ticker=ticker,
        as_of=as_of,
        sector=sector_name,
        benchmarks=benchmarks,
        computed_at=datetime.now(timezone.utc),
        cached=False,
        **snapshot,
    )

    # Cache the body without `cached`: that flag describes the retrieval,
    # not the data, and is set on the way out (True on a hit, False here).
    if redis is not None:
        try:
            body = response.model_dump(mode="json", by_alias=True,
                                       exclude={"cached", "session_so_far"})
            await set_cached_indicators(redis, ticker, body)
        except Exception as e:
            logger.warning(f"Indicators cache write failed for {ticker}: {e}")

    # Part 4.8b-de: today so far is read at request time, never cached.
    session = await session_so_far_on_read(redis, provider, ticker)
    if session is not None:
        response.session_so_far = SessionSoFarOut.model_validate(session)
    return response
