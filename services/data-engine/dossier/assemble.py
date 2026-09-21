"""
TradingFirm — Dossier assembly (Part 2.4). Spec: docs/specs/2.4.md.

One `assemble()` per request: read the stored bars, refresh them if they are
stale, then fan out over the context sources concurrently and fold whatever
came back into one document.

Two rules run through everything here (spec decisions 7 and 8):

  * An upstream failure is a section status, never an HTTP error. Each
    builder runs inside `run_section()`, which maps the typed client errors
    onto `reason` codes, starts the source's cooldown when it was refused,
    and returns an `error` section carrying the same empty payload key its
    `ok` form has.
  * A *database* failure is not a section. `db.DB_ERRORS` is re-raised
    through the boundary and `assemble()` lets it out, so the endpoint can
    answer 503 for the whole document rather than serving half of one.

Budget: every section is bounded by SECTION_TIMEOUT, the fan-out by
FANOUT_BUDGET (a guard against a builder that never yields — with the inner
bound in place the gather cannot reach it), the refresh step by
REFRESH_BUDGET, and the calls actually made are reported back in `budget`.
"""

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from cache import (
    SOURCE_ALPHAVANTAGE,
    SOURCE_EDGAR,
    SOURCE_FINNHUB,
    TTL_COOLDOWN_ALPHAVANTAGE,
    TTL_COOLDOWN_EDGAR,
    TTL_COOLDOWN_FINNHUB,
    cooldown_remaining,
    start_cooldown,
)
from db import DB_ERRORS
from dossier import HORIZON_PROFILES, MAX_FILINGS, MAX_HEADLINES
from dossier.models import (
    REASON_AUTH,
    REASON_BLOCKED,
    REASON_COOLDOWN,
    REASON_RATE_LIMITED,
    REASON_TIMEOUT,
    REASON_UPSTREAM,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_STALE,
    STATUS_TRUNCATED,
    STATUS_UNCONFIGURED,
    BarsSection,
    Budget,
    DossierResponse,
    EarningsSection,
    EventsSection,
    FilingsSection,
    IndicatorsSection,
    NewsSection,
    ProfileSection,
    RecommendationsSection,
    Sections,
)
from dossier.sections import NoBarsStored, indicators_body
from providers.context.alphavantage_client import AlphaVantageError
from providers.context.edgar_client import (
    EdgarError,
    EdgarNotConfigured,
    EdgarRateLimited,
)
from providers.context.finnhub_client import (
    FinnhubAuthError,
    FinnhubError,
    FinnhubNotConfigured,
    FinnhubRateLimited,
)
from tickers import validate_ticker

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Budgets (seconds). See spec decision 12.
SECTION_TIMEOUT = 8.0
FANOUT_BUDGET = 25.0
REFRESH_BUDGET = 20.0

# Source names (also the cooldown names and the `bySource` keys) and the
# cooldown windows live in cache.py, with the other cache configuration.
SOURCE_YFINANCE = "yfinance"

MARKET_CLOSE_HOUR = 16


class SourceCooldown(Exception):
    """The source refused us recently; skip it before any HTTP."""

    def __init__(self, source: str, remaining: int):
        super().__init__(f"{source}: cooldown, {remaining}s left")
        self.source = source
        self.remaining = remaining


@dataclass
class DossierContext:
    """Everything a section builder may touch. Injected rather than reached
    for, so the assembly is testable without FastAPI or a live client."""

    pool: Any = None
    redis: Any = None
    cooldowns: Any = None
    finnhub: Any = None
    edgar: Any = None
    av_client: Any = None
    provider: Any = None
    # (ticker) -> dict with at least {"dailyBars": int}; injected by the
    # endpoint so the assembly never imports main.
    refresh: Optional[Callable[[str], Awaitable[dict]]] = None
    now: Optional[datetime] = None
    calls: dict = field(default_factory=dict)
    _marks: dict = field(default_factory=dict)

    def count(self, source: str, n: int = 1) -> None:
        """Add calls a caller counted itself (the refresh helper reports the
        provider calls it made; nothing else uses this)."""
        if n:
            self.calls[source] = self.calls.get(source, 0) + n

    def mark_clients(self) -> None:
        """Snapshot each HTTP client's call counter.

        One delta per source for the whole dossier, not one per section:
        sections run concurrently and share a client, so a per-section
        `before`/`after` pair counts calls the *other* sections made in
        between (the 2.5 live check reported 14 upstream calls for 10).

        KNOWN LIMIT: `calls_made` is per client object, and the app holds one
        client per process, so two dossiers assembled at the same moment
        still cross-count each other. Acceptable while one caller uses the
        endpoint; a contextvars counter inside the clients is the fix if that
        changes (deferred, docs/progress.md).
        """
        self._marks = {
            SOURCE_FINNHUB: getattr(self.finnhub, "calls_made", 0),
            SOURCE_EDGAR: getattr(self.edgar, "calls_made", 0),
        }

    def collect_clients(self) -> None:
        """Fold the marked clients' deltas into `calls`."""
        for source, client in ((SOURCE_FINNHUB, self.finnhub), (SOURCE_EDGAR, self.edgar)):
            spent = getattr(client, "calls_made", 0) - self._marks.get(source, 0)
            self.count(source, spent)

    def now_utc(self) -> datetime:
        return self.now or datetime.now(timezone.utc)


# ── Staleness (weekday rule; holidays deferred to Phase 3) ───────────────

def reference_session(now: datetime) -> date:
    """The most recent session whose close we can expect to hold bars for:
    today if it is a weekday past 16:00 ET, else the most recent earlier
    weekday. Holidays are not modelled — hence `staleWeekdays`."""
    et = now.astimezone(ET)
    day = et.date()
    if day.weekday() < 5 and et.hour >= MARKET_CLOSE_HOUR:
        return day
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def weekdays_between(after: date, through: date) -> int:
    """Weekdays in the half-open range (after, through]. 0 when the second
    date is not later than the first."""
    if through <= after:
        return 0
    start = after + timedelta(days=1)
    days = (through - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def is_stale(last_bar: Optional[date], now: datetime) -> tuple[bool, int]:
    """(stale, weekdays behind). Stale means more than one weekday behind
    the reference session — one day behind is a normal end-of-day state."""
    if last_bar is None:
        return True, 0
    behind = weekdays_between(last_bar, reference_session(now))
    return behind > 1, behind


# ── The section boundary ─────────────────────────────────────────────────

def safe_detail(exc: Exception) -> str:
    """A short, loggable reason. URLs are dropped: EDGAR messages carry the
    request URL, and G14 keeps every URL out of a response body."""
    words = [w for w in str(exc).split() if "://" not in w and "?" not in w]
    text = " ".join(words).strip(" :")
    return f"{type(exc).__name__}: {text}"[:200] if text else type(exc).__name__


async def _cooldown_for(exc: Exception, ctx: DossierContext) -> None:
    """Start the source's cooldown when the failure was a refusal."""
    if isinstance(exc, FinnhubRateLimited):
        await start_cooldown(ctx.redis, ctx.cooldowns, SOURCE_FINNHUB, TTL_COOLDOWN_FINNHUB)
    elif isinstance(exc, EdgarRateLimited):
        await start_cooldown(ctx.redis, ctx.cooldowns, SOURCE_EDGAR, TTL_COOLDOWN_EDGAR)
    elif isinstance(exc, AlphaVantageError) and type(exc).__name__ in (
        "AlphaVantageCapped", "AlphaVantageRateLimited"
    ):
        await start_cooldown(
            ctx.redis, ctx.cooldowns, SOURCE_ALPHAVANTAGE, TTL_COOLDOWN_ALPHAVANTAGE
        )


def _status_and_reason(exc: Exception) -> tuple[str, Optional[str]]:
    if isinstance(exc, (FinnhubNotConfigured, EdgarNotConfigured)):
        return STATUS_UNCONFIGURED, None
    if isinstance(exc, SourceCooldown):
        return STATUS_ERROR, REASON_COOLDOWN
    if isinstance(exc, asyncio.TimeoutError):
        return STATUS_ERROR, REASON_TIMEOUT
    if isinstance(exc, FinnhubRateLimited):
        return STATUS_ERROR, REASON_RATE_LIMITED
    if isinstance(exc, EdgarRateLimited):
        return STATUS_ERROR, REASON_BLOCKED
    if isinstance(exc, FinnhubAuthError):
        return STATUS_ERROR, REASON_AUTH
    return STATUS_ERROR, REASON_UPSTREAM


async def run_section(
    name: str,
    ctx: DossierContext,
    builder: Callable[[], Awaitable[Any]],
    section_cls,
    *,
    timeout: Optional[float] = None,
):
    """
    Run one section builder under its own timeout and error boundary.

    Database errors are re-raised untouched: they are a 503 for the whole
    dossier, never a degraded section. Everything else becomes an `error`
    (or `unconfigured`) section of `section_cls`, which carries that
    section's payload key in its empty form.
    """
    try:
        # Read the module constant at call time, not at def time: the budget
        # is a knob the endpoint and the tests turn.
        return await asyncio.wait_for(builder(), timeout or SECTION_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as e:
        # Before DB_ERRORS on purpose: TimeoutError sits in the OSError tree.
        logger.warning(f"dossier {name}: timeout after {timeout or SECTION_TIMEOUT}s")
        return section_cls(status=STATUS_ERROR, reason=REASON_TIMEOUT, detail=safe_detail(e))
    except DB_ERRORS:
        raise
    except Exception as e:
        status, reason = _status_and_reason(e)
        await _cooldown_for(e, ctx)
        logger.warning(f"dossier {name}: {status}/{reason} — {safe_detail(e)}")
        detail = None if status == STATUS_UNCONFIGURED else safe_detail(e)
        return section_cls(status=status, reason=reason, detail=detail)


async def _check_cooldown(ctx: DossierContext, source: str, ttl: int) -> None:
    left = await cooldown_remaining(ctx.redis, ctx.cooldowns, source, ttl)
    if left is not None:
        raise SourceCooldown(source, left)


# ── Section builders ─────────────────────────────────────────────────────

async def build_indicators(ctx: DossierContext, ticker: str) -> IndicatorsSection:
    """The Part 1.7 snapshot from stored bars. No upstream call: a DB read
    that raises is a 503, and no bars at all is an `error` here only because
    the 404 decision was already made before the fan-out."""
    body = await indicators_body(ctx.pool, ctx.redis, ticker)
    return IndicatorsSection.model_validate(
        {**body.model_dump(by_alias=True), "status": STATUS_OK}
    )


async def build_news(ctx: DossierContext, ticker: str, profile: dict) -> NewsSection:
    """One Finnhub call, stored through `upsert_news`, then presented from
    the same normalized rows (items missing a url/headline/timestamp are
    dropped by `news_records`, so they are not shown either)."""
    from providers.context.finnhub import company_news, news_records

    await _check_cooldown(ctx, SOURCE_FINNHUB, TTL_COOLDOWN_FINNHUB)
    raw = await company_news(ctx.finnhub, ticker, redis=ctx.redis, days=profile["news_days"],
                             today=ctx.now_utc().date())

    rows = news_records(ticker, raw)
    if ctx.pool is not None and rows:
        from db import upsert_news
        await upsert_news(ctx.pool, rows)

    rows.sort(key=lambda r: r["published_at"], reverse=True)
    truncated = len(rows) > MAX_HEADLINES
    kept = rows[:MAX_HEADLINES]

    # Part 4.4: the rows are presented from the fetch, so the store is asked
    # for each kept headline's id and any label it already carries. A raise
    # here is a database failure like any other (DB_ERRORS -> 503); no pool
    # means ids stay None and ai-agent classifies without write-back.
    labels: dict[str, dict] = {}
    if ctx.pool is not None and kept:
        from db import get_news_labels
        labels = await get_news_labels(ctx.pool, ticker, [r["url"] for r in kept])
    if truncated:
        logger.info(f"dossier news {ticker}: {len(rows)} headlines capped to {MAX_HEADLINES}")
    return NewsSection(
        status=STATUS_TRUNCATED if truncated else STATUS_OK,
        items=[
            {
                "id": labels.get(r["url"], {}).get("id"),
                "publishedAt": r["published_at"],
                "source": r["source"],
                "headline": r["title"],
                "summary": r["summary"],
                "url": r["url"],
                "sentiment": labels.get(r["url"], {}).get("sentiment"),
            }
            for r in kept
        ],
        count=len(kept),
        truncated=truncated,
    )


async def build_events(ctx: DossierContext, ticker: str, profile: dict) -> EventsSection:
    """Two Finnhub calls written into `data_engine.events`, then the section
    is read back from the store — that is what folds 2.3's `meta.earnings`
    rows and 2.1's projections into one list. If the fetches fail the read
    still runs, so the section stays `ok` from what is stored."""
    from providers.context.finnhub import (
        calendar_events,
        earnings_calendar,
        earnings_surprises,
        surprise_events,
    )

    fetch_error: Optional[Exception] = None
    try:
        await _check_cooldown(ctx, SOURCE_FINNHUB, TTL_COOLDOWN_FINNHUB)
        cal_raw = await earnings_calendar(ctx.finnhub, ticker, redis=ctx.redis,
                                          today=ctx.now_utc().date())
        sur_raw = await earnings_surprises(ctx.finnhub, ticker, redis=ctx.redis)
        rows = calendar_events(ticker, cal_raw) + surprise_events(ticker, sur_raw)
        if ctx.pool is not None and rows:
            from db import upsert_events
            await upsert_events(ctx.pool, rows)
    except DB_ERRORS:
        raise
    except Exception as e:
        fetch_error = e
        await _cooldown_for(e, ctx)
        logger.warning(f"dossier events {ticker}: fetch failed, serving the store — {safe_detail(e)}")

    if ctx.pool is None:
        if fetch_error is not None:
            raise fetch_error
        return EventsSection(items=[])

    from db import get_events

    now = ctx.now_utc()
    window = timedelta(days=profile["events_window_days"])
    rows = await get_events(ctx.pool, ticker, since=now - window, until=now + window)
    if fetch_error is not None and not rows:
        # Nothing was fetched and the store has nothing either: report the
        # source's failure (an empty key reads as `unconfigured`) rather than
        # an empty `ok` section that claims we looked and there was nothing.
        raise fetch_error
    return EventsSection(
        status=STATUS_OK,
        items=[{"type": r["event_type"], "at": r["event_at"], "meta": r["meta"]} for r in rows],
    )


async def build_recommendations(ctx: DossierContext, ticker: str) -> RecommendationsSection:
    from providers.context.finnhub import recommendations

    await _check_cooldown(ctx, SOURCE_FINNHUB, TTL_COOLDOWN_FINNHUB)
    rows = await recommendations(ctx.finnhub, ticker, redis=ctx.redis)
    return RecommendationsSection(status=STATUS_OK, items=list(rows))


async def build_profile(ctx: DossierContext, ticker: str) -> ProfileSection:
    from providers.context.finnhub import profile as fetch_profile

    await _check_cooldown(ctx, SOURCE_FINNHUB, TTL_COOLDOWN_FINNHUB)
    raw = await fetch_profile(ctx.finnhub, ticker, redis=ctx.redis)
    return ProfileSection(
        status=STATUS_OK,
        name=raw.get("name"),
        country=raw.get("country"),
        currency=raw.get("currency"),
        exchange=raw.get("exchange"),
        industry=raw.get("finnhubIndustry"),
        ipo=raw.get("ipo") or None,
        market_cap=raw.get("marketCapitalization"),
        shares_outstanding=raw.get("shareOutstanding"),
        weburl=raw.get("weburl"),
        logo=raw.get("logo"),
    )


async def build_filings(ctx: DossierContext, ticker: str, profile: dict) -> FilingsSection:
    """Up to two EDGAR calls, read-only (storing filings stays ingest work).
    `truncated` is one flag: the 10-cap applied, or 2.2 said the `recent`
    block did not reach back far enough."""
    from providers.context.edgar import recent_filings

    await _check_cooldown(ctx, SOURCE_EDGAR, TTL_COOLDOWN_EDGAR)
    rows, block_truncated = await recent_filings(
        ctx.edgar,
        ticker,
        forms=profile["filing_forms"],
        days=profile["filing_days"],
        redis=ctx.redis,
        today=ctx.now_utc().date(),
    )

    rows = sorted(rows, key=lambda r: r.get("filed_on") or "", reverse=True)
    capped = len(rows) > MAX_FILINGS
    kept = rows[:MAX_FILINGS]
    truncated = capped or bool(block_truncated)
    if truncated:
        logger.info(
            f"dossier filings {ticker}: truncated (cap applied: {capped}, "
            f"block short: {bool(block_truncated)})"
        )
    return FilingsSection(
        status=STATUS_TRUNCATED if truncated else STATUS_OK,
        rows=[
            {
                "form": r.get("form"),
                "filedOn": r.get("filed_on"),
                "acceptedAt": r.get("accepted_at"),
                "accession": r.get("accession"),
                "url": r.get("url"),
                "reportDate": (r.get("meta") or {}).get("reportDate") or None,
                "description": (r.get("meta") or {}).get("primaryDocDescription"),
                "items": (r.get("meta") or {}).get("items"),
            }
            for r in kept
        ],
        truncated=truncated,
    )


async def build_earnings(ctx: DossierContext, ticker: str, profile: dict) -> EarningsSection:
    """Part 2.3, passed through untouched: no upstream call, two DB reads,
    `reactions: null` and `dataQuality` exactly as computed there."""
    from providers.context.earnings import earnings_reaction_history

    result = await earnings_reaction_history(ctx.pool, ticker, limit=profile["reactions"])
    return EarningsSection(
        status=STATUS_OK,
        reactions=result.get("reactions"),
        data_quality=result.get("dataQuality") or {},
    )


# ── Bars + refresh, before the fan-out ───────────────────────────────────

async def bars_step(ctx: DossierContext, ticker: str, profile: dict) -> BarsSection:
    """
    Read the stored daily bars, refresh once if they are stale, and describe
    the result. Raises NoBarsStored when the ticker has no bars and the
    refresh produced none — the endpoint's 404. Database errors propagate.
    """
    from db import get_bars

    interval = profile["bars_interval"]
    rows = await get_bars(ctx.pool, ticker, interval)
    last = rows[-1]["ts"].date() if rows else None
    stale, behind = is_stale(last, ctx.now_utc())

    refreshed = False
    if stale and ctx.refresh is not None:
        try:
            outcome = await asyncio.wait_for(ctx.refresh(ticker), REFRESH_BUDGET)
            refreshed = True
            # The refresh helper is the only caller that knows what it spent
            # on yfinance and Alpha Vantage: it returns (body, {source: n}).
            if isinstance(outcome, tuple) and len(outcome) == 2 and isinstance(outcome[1], dict):
                for source, spent in outcome[1].items():
                    ctx.count(source, spent)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            logger.warning(f"dossier bars {ticker}: refresh over its {REFRESH_BUDGET}s budget")
        except DB_ERRORS:
            raise
        except Exception as e:
            # A refusal, a cooldown, a provider error: serve what is stored.
            logger.warning(f"dossier bars {ticker}: refresh not applied — {safe_detail(e)}")
        if refreshed:
            rows = await get_bars(ctx.pool, ticker, interval)
            last = rows[-1]["ts"].date() if rows else None
            stale, behind = is_stale(last, ctx.now_utc())

    if last is None:
        raise NoBarsStored(ticker)

    return BarsSection(
        status=STATUS_STALE if stale else STATUS_OK,
        interval=interval,
        last_bar_date=last,
        stale_weekdays=behind,
        refreshed=refreshed,
    )


# ── The assembler ────────────────────────────────────────────────────────

async def assemble(
    ctx: DossierContext,
    ticker: str,
    horizon: str,
    *,
    fanout_budget: float = FANOUT_BUDGET,
) -> DossierResponse:
    """
    Build one dossier. Raises `ValueError` on a bad ticker or horizon,
    `NoBarsStored` when there is nothing to build a document about, and
    `db.DB_ERRORS` when the database failed — everything else is a section
    status and a 200.
    """
    t = validate_ticker(ticker)
    if horizon not in HORIZON_PROFILES:
        raise ValueError(f"Unknown horizon: {horizon!r}")
    profile = HORIZON_PROFILES[horizon]

    started = _time.perf_counter()
    ctx.mark_clients()
    bars = await bars_step(ctx, t, profile)

    builders = [
        ("indicators", lambda: build_indicators(ctx, t), IndicatorsSection),
        ("news", lambda: build_news(ctx, t, profile), NewsSection),
        ("events", lambda: build_events(ctx, t, profile), EventsSection),
        ("recommendations", lambda: build_recommendations(ctx, t), RecommendationsSection),
        ("filings", lambda: build_filings(ctx, t, profile), FilingsSection),
        ("earnings", lambda: build_earnings(ctx, t, profile), EarningsSection),
        ("profile", lambda: build_profile(ctx, t), ProfileSection),
    ]

    tasks = [
        asyncio.create_task(run_section(name, ctx, builder, cls))
        for name, builder, cls in builders
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), fanout_budget
        )
    except asyncio.TimeoutError:
        # Defensive only: with a per-section bound in place the gather cannot
        # reach here. Whatever is unfinished becomes a `timeout` section.
        results = []
        for (name, _b, cls), task in zip(builders, tasks):
            if task.done() and not task.cancelled():
                results.append(task.exception() or task.result())
            else:
                task.cancel()
                logger.warning(f"dossier {name}: cut off by the fan-out budget")
                results.append(cls(status=STATUS_ERROR, reason=REASON_TIMEOUT,
                                   detail="fan-out budget exceeded"))

    by_name = {}
    for (name, _builder, cls), result in zip(builders, results):
        if isinstance(result, DB_ERRORS):
            raise result
        if isinstance(result, BaseException):
            raise result
        by_name[name] = result

    sections = Sections(bars=bars, **by_name)
    ctx.collect_clients()
    elapsed_ms = int((_time.perf_counter() - started) * 1000)
    budget = Budget(
        upstream_calls=sum(ctx.calls.values()),
        elapsed_ms=elapsed_ms,
        by_source=dict(sorted(ctx.calls.items())),
    )
    return DossierResponse(
        ticker=t,
        horizon=horizon,
        as_of=sections.indicators.as_of,
        generated_at=ctx.now_utc(),
        cached=False,
        sections=sections,
        budget=budget,
    )


def has_error_section(body: dict) -> bool:
    """True when any section of a JSON dossier body is in `error`. Drives the
    short cache TTL (spec decision 11)."""
    sections = (body or {}).get("sections") or {}
    return any(
        isinstance(s, dict) and s.get("status") == STATUS_ERROR for s in sections.values()
    )
