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
    # 2026-09-24: the side split. below = approaches from below (the level
    # tested as resistance), above = from above (tested as support).
    held_below: int = Field(0, alias="heldBelow")
    broke_below: int = Field(0, alias="brokeBelow")
    held_above: int = Field(0, alias="heldAbove")
    broke_above: int = Field(0, alias="brokeAbove")

    model_config = {"populate_by_name": True}


class ZonesOut(BaseModel):
    support: list[ZoneOut] = []
    resistance: list[ZoneOut] = []


class SwingLowOut(BaseModel):
    """The newest confirmed fractal swing low (Part 4.8a-de): its price and
    the bar's date (`ts.date()`, no timezone conversion)."""
    price: float
    date: str


class SessionSoFarOut(BaseModel):
    """Today so far, while the session trades (Part 4.8b-de, spec 4.8b
    decision 16): not a candle. From the download that dropped the open
    session's row; attached at read time, never cached. Prices in dollars,
    `volumeSoFarShares` in shares, `sessionElapsedFrac` 0-1 of the session's real
    length, `changeVsPriorClosePct` percent, `scaledRvol` a ratio like `rvol`
    (volume so far scaled to a full session over the 20-session mean)."""
    open: float
    high: float
    low: float
    last: float
    volume_so_far_shares: int = Field(..., alias="volumeSoFarShares")
    session_elapsed_frac: Optional[float] = Field(None, alias="sessionElapsedFrac")
    change_vs_prior_close_pct: Optional[float] = Field(None, alias="changeVsPriorClosePct")
    scaled_rvol: Optional[float] = Field(None, alias="scaledRvol")
    in_progress: bool = Field(True, alias="inProgress")

    model_config = {"populate_by_name": True}


class BreakoutOut(BaseModel):
    """The newest bar of the last 5 whose close cleared a zone that has held
    from below at least as often as it broke: the zone's band (prices) and
    the bar's RVOL."""
    date: str
    low: float
    high: float
    bar_rvol: Optional[float] = Field(None, alias="barRvol")

    model_config = {"populate_by_name": True}


class VolumeReadOut(BaseModel):
    """Spec 4.8b decision 2. `Rvol` = a multiple of the 20-bar average
    volume; `Days` = trading days."""
    up_days5_rvol: Optional[float] = Field(None, alias="upDays5Rvol")
    down_days5_rvol: Optional[float] = Field(None, alias="downDays5Rvol")
    breakout: Optional[BreakoutOut] = None
    pullback_days: int = Field(0, alias="pullbackDays")
    pullback_rvol: Optional[float] = Field(None, alias="pullbackRvol")

    model_config = {"populate_by_name": True}


class TrendReadOut(BaseModel):
    """Spec 4.8b decision 3: close > EMA20 > EMA50, EMA20 up over 10 bars
    (and by how many ATRs), the last two fractal swing lows (prices)."""
    stack_up: Optional[bool] = Field(None, alias="stackUp")
    ema20_rising10: Optional[bool] = Field(None, alias="ema20Rising10")
    ema20_slope10_atr: Optional[float] = Field(None, alias="ema20Slope10Atr")
    swing_lows: list[float] = Field(default_factory=list, alias="swingLows")
    higher_lows: bool = Field(False, alias="higherLows")

    model_config = {"populate_by_name": True}


class MomentumReadOut(BaseModel):
    """Spec 4.8b decision 3 (the 09-24 rerun's four numbers)."""
    move30_atr: Optional[float] = Field(None, alias="move30Atr")
    range30_atr: Optional[float] = Field(None, alias="range30Atr")
    closes_below_ema20: Optional[int] = Field(None, alias="closesBelowEma20")
    lower_highs: bool = Field(False, alias="lowerHighs")

    model_config = {"populate_by_name": True}


class RangeReadOut(BaseModel):
    """Spec 4.8b decision 4: the 60 bars before the last one."""
    low: float
    high: float
    pos_frac: Optional[float] = Field(None, alias="posFrac")
    ema20_crosses40: int = Field(0, alias="ema20Crosses40")
    closed_outside: bool = Field(False, alias="closedOutside")

    model_config = {"populate_by_name": True}


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
    # 4.8b-de: null outside market hours or when no download ran this
    # session; set on the way out like `cached`, never stored in a cache body.
    session_so_far: Optional[SessionSoFarOut] = Field(None, alias="sessionSoFar")
    # 4.8b-de: the four read blocks (spec 4.8b decisions 2-4). Null in a
    # body cached before the part, served until its TTL.
    volume_read: Optional[VolumeReadOut] = Field(None, alias="volumeRead")
    trend_read: Optional[TrendReadOut] = Field(None, alias="trendRead")
    momentum_read: Optional[MomentumReadOut] = Field(None, alias="momentumRead")
    range_read: Optional[RangeReadOut] = Field(None, alias="rangeRead")

    benchmarks: BenchmarksOut = BenchmarksOut()
    computed_at: datetime = Field(..., alias="computedAt")
    cached: bool = False

    model_config = {"populate_by_name": True}
