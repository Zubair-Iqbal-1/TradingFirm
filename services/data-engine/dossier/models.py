"""
TradingFirm — Response models for GET /dossier/{ticker} (Part 2.4).

Same aliasing convention as `scanners/models.py` and `indicators/models.py`:
snake_case fields, camelCase JSON via `by_alias`. Two rules specific to this
part (spec 2.4 decisions 1 and 6):

  * Every section carries a `status` ∈ ok / truncated / error / unconfigured,
    and an `error` section still carries its payload key, empty — a consumer
    never branches on a missing key.
  * Every float goes through `nan_to_none`, so NaN and ±inf serialize as JSON
    `null` rather than the invalid `NaN` literal.

`cached` is the only field not part of the Redis-cached body: it describes
the retrieval, not the data, and is set on the way out (the 1.7 convention).
"""

import math
from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from indicators import IndicatorsResponse

# ── Section statuses and error reasons ───────────────────────────────────

STATUS_OK = "ok"
STATUS_TRUNCATED = "truncated"
STATUS_ERROR = "error"
STATUS_UNCONFIGURED = "unconfigured"

REASON_TIMEOUT = "timeout"
REASON_RATE_LIMITED = "rate_limited"
REASON_BLOCKED = "blocked"
REASON_AUTH = "auth"
REASON_COOLDOWN = "cooldown"
REASON_UPSTREAM = "upstream"

# Bars is the one section with a fifth state: the data is real, just old.
STATUS_STALE = "stale"


def nan_to_none(value: Any) -> Any:
    """NaN/±inf → None, recursing into lists and dicts. Everything else is
    returned unchanged."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [nan_to_none(v) for v in value]
    if isinstance(value, dict):
        return {k: nan_to_none(v) for k, v in value.items()}
    return value


class _Base(BaseModel):
    model_config = {"populate_by_name": True}

    @field_validator("*", mode="before")
    @classmethod
    def _clean_floats(cls, value):
        return nan_to_none(value)


class Section(_Base):
    """Fields every section shares. `reason`/`detail` are set only when
    `status` is `error`; `detail` never carries a URL or a key (G14)."""

    status: str = STATUS_OK
    reason: Optional[str] = None
    detail: Optional[str] = None


# ── Sections ─────────────────────────────────────────────────────────────

class BarsSection(Section):
    """What the stored bars looked like, and whether we asked for more.
    `stale` + `refreshed: true` is "we asked, the source had nothing newer"."""

    interval: Optional[str] = None
    last_bar_date: Optional[date] = Field(None, alias="lastBarDate")
    stale_weekdays: Optional[int] = Field(None, alias="staleWeekdays")
    refreshed: bool = False


class IndicatorsSection(IndicatorsResponse):
    """The Part 1.7 snapshot, flattened, plus the section status. The two
    fields IndicatorsResponse requires are optional here: an `error` or
    `unconfigured` section has no snapshot to carry."""

    status: str = STATUS_OK
    reason: Optional[str] = None
    detail: Optional[str] = None
    ticker: str = ""
    computed_at: Optional[datetime] = Field(None, alias="computedAt")

    @field_validator("*", mode="before")
    @classmethod
    def _clean_floats(cls, value):
        return nan_to_none(value)


class NewsItem(_Base):
    # The news_items row id (Part 4.4): what ai-agent's classifier writes a
    # label back to. None when the store could not be read (no pool).
    id: Optional[int] = None
    published_at: Optional[datetime] = Field(None, alias="publishedAt")
    source: Optional[str] = None
    headline: Optional[str] = None
    summary: Optional[str] = None
    url: Optional[str] = None
    # The stored classification object (Part 4.2's write-back), or None for
    # a headline nobody has labelled yet. Up to one dossier TTL stale.
    sentiment: Optional[dict] = None


class NewsSection(Section):
    items: list[NewsItem] = []
    count: int = 0
    truncated: bool = False


class EventItem(_Base):
    type: Optional[str] = None
    at: Optional[datetime] = None
    meta: dict = {}


class EventsSection(Section):
    items: list[EventItem] = []


class RecommendationsSection(Section):
    """Finnhub's own rows, unreshaped: period, strongBuy, buy, hold, sell,
    strongSell — already camelCase."""

    items: list[dict] = []


class FilingItem(_Base):
    form: Optional[str] = None
    filed_on: Optional[date] = Field(None, alias="filedOn")
    accepted_at: Optional[datetime] = Field(None, alias="acceptedAt")
    accession: Optional[str] = None
    url: Optional[str] = None
    report_date: Optional[date] = Field(None, alias="reportDate")
    description: Optional[str] = None
    items: Optional[str] = None


class FilingsSection(Section):
    rows: list[FilingItem] = []
    # True when the 10-filing cap applied *or* EDGAR's `recent` block did not
    # reach back far enough (2.2's flag). One flag, one meaning: the list is
    # not the complete window.
    truncated: bool = False


class DataQuality(_Base):
    """Part 2.3's object, passed through untouched."""

    source: Optional[str] = None
    dropped: int = 0
    disagreements: int = 0


class EarningsSection(Section):
    # `null` (no confirmed report exists) stays distinct from `[]` (reports
    # exist, no bars explain them) — 2.3 decision 11.
    reactions: Optional[list[dict]] = None
    data_quality: DataQuality = Field(default_factory=DataQuality, alias="dataQuality")


class ProfileSection(Section):
    name: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    exchange: Optional[str] = None
    industry: Optional[str] = None
    ipo: Optional[date] = None
    market_cap: Optional[float] = Field(None, alias="marketCap")
    shares_outstanding: Optional[float] = Field(None, alias="sharesOutstanding")
    weburl: Optional[str] = None
    logo: Optional[str] = None


class Sections(_Base):
    bars: BarsSection = Field(default_factory=BarsSection)
    indicators: IndicatorsSection = Field(default_factory=IndicatorsSection)
    news: NewsSection = Field(default_factory=NewsSection)
    events: EventsSection = Field(default_factory=EventsSection)
    recommendations: RecommendationsSection = Field(default_factory=RecommendationsSection)
    filings: FilingsSection = Field(default_factory=FilingsSection)
    earnings: EarningsSection = Field(default_factory=EarningsSection)
    profile: ProfileSection = Field(default_factory=ProfileSection)


class Budget(_Base):
    """What this dossier cost upstream. `bySource` counts calls actually
    made, so a fully cached dossier reports zeros."""

    upstream_calls: int = Field(0, alias="upstreamCalls")
    elapsed_ms: int = Field(0, alias="elapsedMs")
    by_source: dict[str, int] = Field(default_factory=dict, alias="bySource")


class DossierResponse(_Base):
    ticker: str
    horizon: str
    as_of: Optional[datetime] = Field(None, alias="asOf")
    generated_at: datetime = Field(..., alias="generatedAt")
    cached: bool = False
    sections: Sections = Field(default_factory=Sections)
    budget: Budget = Field(default_factory=Budget)
