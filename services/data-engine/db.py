"""
TradingFirm — Data Engine Database Layer

Async PostgreSQL operations using asyncpg.
Manages connection pooling, scan result persistence,
and stock record upserts.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg

from config import settings

logger = logging.getLogger(__name__)

# Every exception that means "the database, not an upstream source, failed"
# (Part 2.4). Named here so the dossier's section boundary re-raises exactly
# what a test raises: a DB failure is a 503 for the whole document, never a
# degraded section.
#
# ConnectionError, not OSError: a socket dying mid-query is a database
# failure, but the wider OSError family is not. asyncio.TimeoutError *is*
# the builtin TimeoutError, which subclasses OSError — with OSError here,
# every section that ran out of budget would have been reported as a dead
# database. The HTTP clients map their own OSErrors to typed errors, so no
# upstream failure reaches this tuple either.
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError)


# ── Connection Pool ──────────────────────────────────────────────

async def create_db_pool() -> asyncpg.Pool:
    """Create and return an asyncpg connection pool."""
    dsn = settings.asyncpg_url
    logger.info(f"Connecting to database: {dsn.split('@')[1] if '@' in dsn else dsn}")
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    logger.info("Database connection pool created")
    return pool


# ── Scan Results ─────────────────────────────────────────────────

async def save_scan_result(
    pool: asyncpg.Pool,
    scanned_at: datetime,
    market_status: str,
    total_screened: int,
    total_passed: int,
    duration_seconds: float,
    stocks: list[dict],
) -> str:
    """
    Insert a scan result into data_engine.scan_results.
    Returns the generated scan UUID.
    """
    query = """
        INSERT INTO data_engine.scan_results
            (scanned_at, market_status, total_screened, total_passed, duration_seconds, stocks)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id::text
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            query,
            scanned_at,
            market_status,
            total_screened,
            total_passed,
            duration_seconds,
            json.dumps(stocks, default=str),
        )
    scan_id = row["id"]
    logger.info(f"Saved scan result: {scan_id} ({total_passed}/{total_screened} passed)")
    return scan_id


async def get_latest_scan(pool: asyncpg.Pool) -> Optional[dict]:
    """
    Fetch the most recent scan result from the database.
    Returns dict with all fields, or None if no scans exist.
    """
    query = """
        SELECT
            id::text,
            scanned_at,
            market_status,
            total_screened,
            total_passed,
            duration_seconds,
            stocks
        FROM data_engine.scan_results
        ORDER BY scanned_at DESC
        LIMIT 1
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query)

    if row is None:
        return None

    return {
        "id": row["id"],
        "scanned_at": row["scanned_at"].isoformat(),
        "market_status": row["market_status"],
        "total_screened": row["total_screened"],
        "total_passed": row["total_passed"],
        "duration_seconds": row["duration_seconds"],
        "stocks": json.loads(row["stocks"]) if isinstance(row["stocks"], str) else row["stocks"],
    }


async def get_scan_history(pool: asyncpg.Pool, limit: int = 10) -> list[dict]:
    """
    Fetch recent scan metadata (without the full stocks array).
    """
    query = """
        SELECT
            id::text,
            scanned_at,
            market_status,
            total_screened,
            total_passed,
            duration_seconds
        FROM data_engine.scan_results
        ORDER BY scanned_at DESC
        LIMIT $1
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, limit)

    return [
        {
            "id": row["id"],
            "scanned_at": row["scanned_at"].isoformat(),
            "market_status": row["market_status"],
            "total_screened": row["total_screened"],
            "total_passed": row["total_passed"],
            "duration_seconds": row["duration_seconds"],
        }
        for row in rows
    ]


# ── Stock Records ────────────────────────────────────────────────

async def upsert_stocks(pool: asyncpg.Pool, stocks: list[dict]) -> int:
    """
    Upsert stock records into data_engine.stocks.
    Uses ON CONFLICT to update existing records.
    Returns the number of rows upserted.
    """
    if not stocks:
        return 0

    query = """
        INSERT INTO data_engine.stocks
            (ticker, name, sector, industry, market_cap, float_shares, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (ticker) DO UPDATE SET
            name = EXCLUDED.name,
            sector = EXCLUDED.sector,
            industry = EXCLUDED.industry,
            market_cap = EXCLUDED.market_cap,
            float_shares = EXCLUDED.float_shares,
            updated_at = EXCLUDED.updated_at
    """
    now = datetime.now(timezone.utc)
    records = [
        (
            s.get("symbol", s.get("ticker", "")),
            s.get("name", ""),
            s.get("sector", "Other"),
            s.get("industry", "Other"),
            s.get("market_cap", s.get("marketCap", 0)) or 0,
            s.get("float_shares", s.get("floatShares", 0)) or 0,
            now,
        )
        for s in stocks
    ]

    async with pool.acquire() as conn:
        await conn.executemany(query, records)

    logger.info(f"Upserted {len(records)} stocks into data_engine.stocks")
    return len(records)


async def get_stock(pool: asyncpg.Pool, ticker: str) -> Optional[dict]:
    """
    One row from data_engine.stocks (written by the scan pipeline), or
    None if the ticker was never scanned. Read-only.
    """
    query = """
        SELECT ticker, name, sector, industry, market_cap, float_shares, updated_at
        FROM data_engine.stocks
        WHERE ticker = $1
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, ticker)

    if row is None:
        return None
    return {
        "ticker": row["ticker"],
        "name": row["name"],
        "sector": row["sector"],
        "industry": row["industry"],
        "market_cap": row["market_cap"],
        "float_shares": row["float_shares"],
        "updated_at": row["updated_at"],
    }


# ── Context: news + events (Part 2.1) ────────────────────────────

# news_items.ticker is NOT NULL (a nullable column inside a UNIQUE constraint
# does not dedup); general-market news is stored under this sentinel.
MARKET_TICKER = "_MARKET"


async def upsert_news(pool: asyncpg.Pool, items: list[dict]) -> int:
    """
    Insert news rows into data_engine.news_items, skipping any (ticker, url)
    already stored. Each item: ticker (None → MARKET_TICKER), published_at
    (datetime), source, title, url, summary. Items without a url or title
    are dropped and counted in the log. Duplicates inside one batch are
    collapsed before the statement runs.

    Returns the number of rows sent (executemany reports no insert count).
    No explicit transaction: a raise mid-batch can leave earlier rows
    inserted — same deferred defect as upsert_bars(); a rerun dedups.
    """
    if not items:
        return 0

    query = """
        INSERT INTO data_engine.news_items
            (ticker, published_at, source, title, url, summary)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (ticker, url) DO NOTHING
    """
    records: list[tuple] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for item in items:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if not url or not title or item.get("published_at") is None:
            dropped += 1
            continue
        ticker = item.get("ticker") or MARKET_TICKER
        key = (ticker, url)
        if key in seen:
            continue
        seen.add(key)
        records.append((
            ticker,
            item["published_at"],
            (item.get("source") or "")[:100],
            title,
            url,
            item.get("summary") or "",
        ))
    if dropped:
        logger.warning(f"upsert_news: dropped {dropped} item(s) without url/title/published_at")
    if not records:
        return 0

    async with pool.acquire() as conn:
        await conn.executemany(query, records)

    logger.info(f"Sent {len(records)} news rows ({len(items) - len(records)} dup/dropped)")
    return len(records)


GET_MARKET_NEWS_SQL = """
    SELECT id, published_at, source, title, summary, url, sentiment
    FROM data_engine.news_items
    WHERE ticker = $1 AND published_at >= $2
    ORDER BY published_at DESC
    LIMIT $3
"""

# Part 4.2: the classifier's write-back target. The column is REPLACED, never
# merged (spec 4.2 decision 9) — a re-classification is the newer truth, and
# a merge would leave half of an older verdict behind. RETURNING id is what
# tells the route 404 from 200 without a second round trip.
SET_NEWS_SENTIMENT_SQL = """
    UPDATE data_engine.news_items
    SET sentiment = $2::jsonb
    WHERE id = $1
    RETURNING id
"""


# Part 4.4: the dossier's news section is presented from the fetched rows,
# and upsert_news is ON CONFLICT DO NOTHING with no RETURNING, so this read is
# what gives each presented headline its row id (the classifier's write-back
# target) and the label it may already carry.
GET_NEWS_LABELS_SQL = """
    SELECT id, url, sentiment
    FROM data_engine.news_items
    WHERE ticker = $1 AND url = ANY($2::text[])
"""


def decode_sentiment(raw) -> Optional[dict]:
    """jsonb arrives as text (no codec is registered). A sentiment that will
    not parse reads as null rather than failing the whole list — the same
    rule get_events uses for `meta`."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("news_items: unparseable sentiment on a row, returning null")
        return None
    return value if isinstance(value, dict) else None


async def get_news_labels(pool: asyncpg.Pool, ticker: str, urls: list[str]) -> dict[str, dict]:
    """
    {url: {"id", "sentiment"}} for the stored rows of `ticker` among `urls`
    (Part 4.4). Read-only, one statement, served by the (ticker, url) unique
    index. A url with no row is simply absent from the answer.
    """
    if not urls:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(GET_NEWS_LABELS_SQL, ticker, urls)
    return {
        row["url"]: {"id": row["id"], "sentiment": decode_sentiment(row["sentiment"])}
        for row in rows
    }


async def get_market_news(pool: asyncpg.Pool, since: datetime, limit: int) -> list[dict]:
    """
    Market news (ticker = MARKET_TICKER) published at or after `since`,
    newest first, at most `limit` rows (Part 3.6a). Read-only; served by
    news_items_ticker_published_idx.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(GET_MARKET_NEWS_SQL, MARKET_TICKER, since, limit)
    return [dict(row) for row in rows]


async def set_news_sentiment(pool: asyncpg.Pool, news_id: int, sentiment: dict) -> bool:
    """
    Store one classification on data_engine.news_items.sentiment (Part 4.2).

    True when a row was updated, False when `news_id` matches nothing — the
    route turns that into a 404. One statement, one row: no transaction is
    needed and there is no partial state to leave behind.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            SET_NEWS_SENTIMENT_SQL, news_id, json.dumps(sentiment, default=str)
        )
    return row is not None


async def upsert_events(pool: asyncpg.Pool, events: list[dict]) -> int:
    """
    Upsert rows into data_engine.events keyed on (ticker, event_type,
    event_at). `meta` is merged: existing || new, so a writer that owns a
    nested key (e.g. meta.calendar, meta.surprise) replaces only its own
    key and leaves the others intact. Duplicates inside one batch are
    collapsed (last wins) so the statement never touches a row twice.

    Returns the number of rows sent. Same no-transaction caveat as
    upsert_news().
    """
    if not events:
        return 0

    query = """
        INSERT INTO data_engine.events (ticker, event_type, event_at, meta, updated_at)
        VALUES ($1, $2, $3, $4::jsonb, now())
        ON CONFLICT (ticker, event_type, event_at) DO UPDATE SET
            meta = data_engine.events.meta || EXCLUDED.meta,
            updated_at = now()
    """
    by_key: dict[tuple, dict] = {}
    for ev in events:
        key = (ev["ticker"], ev["event_type"], ev["event_at"])
        by_key[key] = ev
    records = [
        (t, et, at, json.dumps(ev.get("meta") or {}, default=str))
        for (t, et, at), ev in by_key.items()
    ]

    async with pool.acquire() as conn:
        await conn.executemany(query, records)

    logger.info(f"Sent {len(records)} event rows")
    return len(records)


async def get_events(
    pool: asyncpg.Pool,
    ticker: str,
    event_type: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> list[dict]:
    """
    Fetch stored events for a ticker, oldest first (Part 2.3).

    Generic on purpose: Part 2.3 reads ('earnings', date) rows, 2.4's
    dossier reads every kind, and 6.4 asks "is there an earnings event in
    the next 24 hours". `event_type`, `since` and `until` are each optional
    filters; `meta` is decoded from jsonb text, and a row whose meta will
    not parse is kept with an empty dict rather than failing the read — a
    malformed meta must not hide the date it belongs to.
    """
    conditions = ["ticker = $1"]
    args: list[Any] = [ticker]
    if event_type is not None:
        args.append(event_type)
        conditions.append(f"event_type = ${len(args)}")
    if since is not None:
        args.append(since)
        conditions.append(f"event_at >= ${len(args)}")
    if until is not None:
        args.append(until)
        conditions.append(f"event_at <= ${len(args)}")

    query = f"""
        SELECT ticker, event_type, event_at, meta
        FROM data_engine.events
        WHERE {' AND '.join(conditions)}
        ORDER BY event_at ASC
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)

    events = []
    for row in rows:
        raw = row["meta"]
        if isinstance(raw, (str, bytes)):
            try:
                meta = json.loads(raw)
            except (ValueError, TypeError):
                logger.warning(
                    f"get_events {ticker}: unparseable meta on "
                    f"{row['event_type']} {row['event_at']}, read as empty"
                )
                meta = {}
        else:
            meta = raw or {}
        if not isinstance(meta, dict):
            meta = {}
        events.append({
            "ticker": row["ticker"],
            "event_type": row["event_type"],
            "event_at": row["event_at"],
            "meta": meta,
        })
    return events


# ── Context: filings (Part 2.2) ──────────────────────────────────

async def upsert_filings(pool: asyncpg.Pool, filings: list[dict]) -> int:
    """
    Insert rows into data_engine.filings, skipping any (ticker, accession)
    already stored — ON CONFLICT DO NOTHING, because a filed accession
    never changes (docs/decisions.md 2026-09-09, Part 2.2). Each item:
    ticker, form, filed_on (date, the official filingDate), accepted_at
    (aware datetime or None), accession, url, meta (dict). Items missing
    ticker / form / filed_on / accession / url are dropped and counted in
    the log; duplicates inside one batch are collapsed (first kept, the
    same answer DO NOTHING gives).

    Returns the number of rows sent (executemany reports no insert count).
    Same no-transaction caveat as upsert_news(): a raise mid-batch can
    leave earlier rows inserted; a rerun dedups.
    """
    if not filings:
        return 0

    query = """
        INSERT INTO data_engine.filings
            (ticker, form, filed_on, accepted_at, accession, url, meta)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        ON CONFLICT (ticker, accession) DO NOTHING
    """
    records: list[tuple] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for item in filings:
        ticker = item.get("ticker") or ""
        form = (item.get("form") or "").strip()
        accession = (item.get("accession") or "").strip()
        url = (item.get("url") or "").strip()
        if not ticker or not form or not accession or not url or item.get("filed_on") is None:
            dropped += 1
            continue
        key = (ticker, accession)
        if key in seen:
            continue
        seen.add(key)
        records.append((
            ticker,
            form[:20],
            item["filed_on"],
            item.get("accepted_at"),
            accession,
            url,
            json.dumps(item.get("meta") or {}, default=str),
        ))
    if dropped:
        logger.warning(f"upsert_filings: dropped {dropped} item(s) missing ticker/form/filed_on/accession/url")
    if not records:
        return 0

    async with pool.acquire() as conn:
        await conn.executemany(query, records)

    logger.info(f"Sent {len(records)} filing rows ({len(filings) - len(records)} dup/dropped)")
    return len(records)


# ── OHLCV Bars ───────────────────────────────────────────────────

def bar_records_from_df(df) -> list[dict]:
    """Convert a provider OHLCV DataFrame (Open/High/Low/Close/Volume,
    datetime index) into the lowercase dict shape upsert_bars() expects."""
    if df is None or df.empty:
        return []
    return [
        {
            "ts": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]),
        }
        for ts, row in df.iterrows()
    ]


async def upsert_bars(
    pool: asyncpg.Pool,
    ticker: str,
    interval: str,
    bars: list[dict],
) -> int:
    """
    Upsert OHLCV bars into data_engine.ohlcv_bars.
    Each bar dict needs: ts (datetime), open, high, low, close, volume.
    Returns the number of rows upserted.
    """
    if not bars:
        return 0

    query = """
        INSERT INTO data_engine.ohlcv_bars
            (ticker, interval, ts, open, high, low, close, volume)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (ticker, interval, ts) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume
    """
    records = [
        (
            ticker,
            interval,
            bar["ts"],
            bar["open"],
            bar["high"],
            bar["low"],
            bar["close"],
            bar["volume"],
        )
        for bar in bars
    ]

    async with pool.acquire() as conn:
        await conn.executemany(query, records)

    logger.info(f"Upserted {len(records)} {interval} bars for {ticker}")
    return len(records)


def bar_record_to_json(bar: dict) -> dict:
    """Convert one row dict from get_bars() (datetime ts) into a
    JSON-ready dict (ISO string ts) for API responses. Read-direction
    counterpart to bar_records_from_df()."""
    return {
        "ts": bar["ts"].isoformat(),
        "open": bar["open"],
        "high": bar["high"],
        "low": bar["low"],
        "close": bar["close"],
        "volume": bar["volume"],
    }


def bars_to_df(bars: list[dict]):
    """Inverse of bar_records_from_df(): row dicts from get_bars() → a
    provider-shaped DataFrame (Open/High/Low/Close/Volume columns,
    DatetimeIndex named 'Date', oldest first). Empty list → empty frame
    with the same columns."""
    import pandas as pd

    columns = ["Open", "High", "Low", "Close", "Volume"]
    if not bars:
        return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], name="Date"))
    index = pd.DatetimeIndex([b["ts"] for b in bars], name="Date")
    return pd.DataFrame(
        {
            "Open": [float(b["open"]) for b in bars],
            "High": [float(b["high"]) for b in bars],
            "Low": [float(b["low"]) for b in bars],
            "Close": [float(b["close"]) for b in bars],
            "Volume": [int(b["volume"]) for b in bars],
        },
        index=index,
    )


async def get_bars(
    pool: asyncpg.Pool,
    ticker: str,
    interval: str,
    since: Optional[datetime] = None,
) -> list[dict]:
    """
    Fetch stored OHLCV bars for a ticker/interval, ordered oldest first.
    If `since` is given, only bars with ts >= since are returned.
    """
    if since is not None:
        query = """
            SELECT ts, open, high, low, close, volume
            FROM data_engine.ohlcv_bars
            WHERE ticker = $1 AND interval = $2 AND ts >= $3
            ORDER BY ts ASC
        """
        args = (ticker, interval, since)
    else:
        query = """
            SELECT ts, open, high, low, close, volume
            FROM data_engine.ohlcv_bars
            WHERE ticker = $1 AND interval = $2
            ORDER BY ts ASC
        """
        args = (ticker, interval)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)

    return [
        {
            "ts": row["ts"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for row in rows
    ]
