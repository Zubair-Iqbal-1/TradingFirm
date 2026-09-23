"""
TradingFirm — Response model for GET /indicators/{ticker} (Part 1.7).

Same aliasing convention as scanners/models.py: snake_case fields, camelCase
JSON via `by_alias`. `cached` is the only field that is *not* part of the
Redis-cached body — it is set after retrieval (see main.py), so a cache hit
can say `cached: true` while the stored body stays identical.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ZoneOut(BaseModel):
    low: float
    high: float
    price: float
    score: int
    methods: list[str] = []
    tests: int = 0
    recent: bool = False
    volume_node: bool = Field(False, alias="volumeNode")
    # Part 4.8a-de: the level's history over the full stored series
    # (spec decision 2). Defaults so a cached pre-part body still validates.
    touches: int = 0
    held: int = 0
    broke: int = 0
    last_touch: Optional[str] = Field(None, alias="lastTouch")

    model_config = {"populate_by_name": True}


class ZonesOut(BaseModel):
    support: list[ZoneOut] = []
    resistance: list[ZoneOut] = []


class SwingLowOut(BaseModel):
    """The newest confirmed fractal swing low (Part 4.8a-de): its price and
    the bar's date (`ts.date()`, no timezone conversion)."""
    price: float
    date: str


class BenchmarkOut(BaseModel):
    """Which benchmark series was used and how many stored bars it had.
    `bars == 0` means the RS fields that need it are null."""
    ticker: Optional[str] = None
    bars: int = 0


class BenchmarksOut(BaseModel):
    spy: BenchmarkOut = BenchmarkOut()
    sector: BenchmarkOut = BenchmarkOut()


class IndicatorsResponse(BaseModel):
    ticker: str
    as_of: Optional[datetime] = Field(None, alias="asOf")
    bars: int = 0
    close: Optional[float] = None
    sector: Optional[str] = None

    ema20: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    atr14: Optional[float] = None
    rvol: float = 0.0
    rsi14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = Field(None, alias="macdSignal")
    macd_hist: Optional[float] = Field(None, alias="macdHist")
    pos_52w: Optional[float] = Field(None, alias="pos52w")
    ext20: Optional[float] = None
    ext50: Optional[float] = None
    rs_spy_5: Optional[float] = Field(None, alias="rsSpy5")
    rs_spy_20: Optional[float] = Field(None, alias="rsSpy20")
    rs_sector_5: Optional[float] = Field(None, alias="rsSector5")
    rs_sector_20: Optional[float] = Field(None, alias="rsSector20")
    avg_dollar_volume_20: Optional[float] = Field(None, alias="avgDollarVolume20")
    gap_pct: Optional[float] = Field(None, alias="gapPct")
    gaps20: list[Optional[float]] = []
    zones: ZonesOut = ZonesOut()
    # ai-agent projects this key as-is (spec 4.8a-de decision 3); both
    # indicator pin tests list it. None with fewer than 5 bars or no pivot.
    last_swing_low: Optional[SwingLowOut] = Field(None, alias="lastSwingLow")

    benchmarks: BenchmarksOut = BenchmarksOut()
    computed_at: datetime = Field(..., alias="computedAt")
    cached: bool = False

    model_config = {"populate_by_name": True}
