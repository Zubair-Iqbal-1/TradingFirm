"""Part 4.8b-ai — the reads: the uptrend call, the six flags at their
starting lines, the partial-bar guard, and the eleven stored verdicts from
the committed fixture. Pure; no I/O beyond reading the fixture."""

import copy
import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import reads

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 9, 21)
AFTER_CLOSE = datetime(2026, 9, 21, 17, 0, tzinfo=ET)
IN_SESSION = datetime(2026, 9, 21, 15, 0, tzinfo=ET)
FIXTURE = Path(__file__).parent / "fixtures" / "reads_eleven.json"


def indicators(**over):
    """A quiet, complete section: no flag fires, the uptrend holds."""
    base = {
        "asOf": "2026-09-18T00:00:00Z", "close": 52.0, "ema20": 50.0, "ema50": 48.0,
        "rsSpy20": 1.5, "rsSpy5": -9.0,
        "volumeRead": {"upDays5Rvol": 1.1, "downDays5Rvol": 1.0,
                       "breakout": {"date": "2026-09-17", "low": 51.0, "high": 51.4, "barRvol": 1.6},
                       "pullbackDays": 0, "pullbackRvol": None},
        "trendRead": {"stackUp": True, "ema20Rising10": True, "ema20Slope10Atr": 0.41,
                      "swingLows": [45.1, 47.3], "higherLows": True},
        "momentumRead": {"move30Atr": 2.5, "range30Atr": 6.0, "closesBelowEma20": 2, "lowerHighs": False},
        "rangeRead": {"low": 40.0, "high": 55.0, "posFrac": 0.85, "ema20Crosses40": 2, "closedOutside": False},
        "sessionSoFar": {"open": 52.1, "high": 53.0, "low": 51.9, "last": 52.8, "volumeSoFarShares": 100,
                         "sessionElapsedFrac": 0.3, "changeVsPriorClosePct": 1.5, "scaledRvol": 1.9,
                         "inProgress": True},
    }
    for path, value in over.items():
        block, _, key = path.partition("__")
        if key:
            base[block][key] = value
        else:
            base[block] = value
    return base


def build(**over):
    return reads.build(indicators(**over), today=TODAY, now=AFTER_CLOSE)


# ── The version and the lines ────────────────────────────────────

def test_reads_version_is_pinned():
    """A line moves only with a READS_VERSION bump (spec 4.8b-ai decision 1)."""
    assert (reads.READS_VERSION, reads.THRESHOLDS) == (1, (1.0, 1.5, 2, 0.7, 1.5, 5, -1.5, 10, 0.2, 0.8, 5))
    assert reads.FLAGS == ("lowVolumeBreakout", "distribution", "dryPullback", "dead", "bleeding", "rangeBound")
    assert reads.VOLUME_FLAGS == {"lowVolumeBreakout", "distribution", "dryPullback"}


def test_quiet_section_raises_nothing():
    out = build()
    assert out == {"uptrend": True, "trendReasons": out["trendReasons"], "flags": [],
                   "withheld": [], "lastBarPartial": False}
    assert out["trendReasons"] == [
        "close 52.00 > EMA20 50.00 > EMA50 48.00 ✓",
        "EMA20 rising over 10 bars ✓ (+0.41 ATR)",
        "RS 20d vs SPY +1.50 % ✓",
        "higher swing lows ✓ (45.10 → 47.30)",
    ]


@pytest.mark.parametrize("flag, at, under, over", [
    # (flag, fires-at-the-line?, just under the line, just over the line) as overrides
    ("lowVolumeBreakout", ("volumeRead__breakout", {"date": "d", "low": 1, "high": 2, "barRvol": 1.0}, False),
     ("volumeRead__breakout", {"date": "d", "low": 1, "high": 2, "barRvol": 0.99}, True),
     ("volumeRead__breakout", {"date": "d", "low": 1, "high": 2, "barRvol": 1.01}, False)),
    ("distribution", ("volumeRead__downDays5Rvol", 1.5, False),        # up5 is 1.0 → line at 1.5
     ("volumeRead__downDays5Rvol", 1.49, False), ("volumeRead__downDays5Rvol", 1.51, True)),
    ("dead", ("momentumRead__move30Atr", 1.5, False),                 # range 4 → only the move decides
     ("momentumRead__move30Atr", 1.49, True), ("momentumRead__move30Atr", 1.51, False)),
    ("bleeding", ("momentumRead__move30Atr", -1.5, True),             # below 10, lowerHighs set
     ("momentumRead__move30Atr", -1.49, False), ("momentumRead__move30Atr", -1.51, True)),
    ("rangeBound", ("rangeRead__posFrac", 0.8, True),                 # crosses 5
     ("rangeRead__posFrac", 0.81, False), ("rangeRead__posFrac", 0.79, True)),
])
def test_flag_boundaries(flag, at, under, over):
    """Each line at, just under and just over (spec 4.8b decision 5)."""
    fixed = {}
    if flag == "distribution":
        fixed = {"volumeRead__upDays5Rvol": 1.0}
    if flag == "dead":
        fixed = {"momentumRead__range30Atr": 4.0}
    if flag == "bleeding":
        fixed = {"momentumRead__closesBelowEma20": 10, "momentumRead__lowerHighs": True}
    if flag == "rangeBound":
        fixed = {"rangeRead__ema20Crosses40": 5}
    for path, value, expected in (at, under, over):
        out = build(**fixed, **{path: value})
        assert (flag in out["flags"]) is expected, (path, value)


def test_dry_pullback_boundaries():
    assert "dryPullback" in build(volumeRead__pullbackDays=2, volumeRead__pullbackRvol=0.69)["flags"]
    assert "dryPullback" not in build(volumeRead__pullbackDays=2, volumeRead__pullbackRvol=0.7)["flags"]
    assert "dryPullback" not in build(volumeRead__pullbackDays=1, volumeRead__pullbackRvol=0.2)["flags"]
    assert "dryPullback" in build(volumeRead__pullbackDays=3, volumeRead__pullbackRvol=0.5)["flags"]


def test_flags_listed_in_table_order():
    out = build(volumeRead__breakout={"date": "d", "low": 1, "high": 2, "barRvol": 0.5},
                momentumRead__move30Atr=0.1, momentumRead__range30Atr=2.0,
                rangeRead__posFrac=0.5, rangeRead__ema20Crosses40=9)
    assert out["flags"] == ["lowVolumeBreakout", "dead", "rangeBound"]


def test_range_bound_needs_a_close_inside():
    assert "rangeBound" in build(rangeRead__posFrac=0.5, rangeRead__ema20Crosses40=6)["flags"]
    assert "rangeBound" not in build(rangeRead__posFrac=0.5, rangeRead__ema20Crosses40=6,
                                     rangeRead__closedOutside=True)["flags"]
    assert "rangeBound" not in build(rangeRead__posFrac=0.5, rangeRead__ema20Crosses40=4)["flags"]


# ── The uptrend ──────────────────────────────────────────────────

@pytest.mark.parametrize("path, value, reason", [
    ("trendRead__stackUp", False, "close 52.00 > EMA20 50.00 > EMA50 48.00 ✗"),
    ("trendRead__ema20Rising10", False, "EMA20 rising over 10 bars ✗ (+0.41 ATR)"),
    ("rsSpy20", -0.3, "RS 20d vs SPY -0.30 % ✗"),
    ("trendRead__higherLows", False, "higher swing lows ✗ (45.10 → 47.30)"),
])
def test_uptrend_needs_all_four(path, value, reason):
    out = build(**{path: value})
    assert out["uptrend"] is False and reason in out["trendReasons"]


def test_uptrend_null_rs_fails():
    """A null RS is a failed check, never a pass (spec 4.8b decision 5)."""
    out = build(rsSpy20=None)
    assert out["uptrend"] is False and "RS 20d vs SPY unknown ✗" in out["trendReasons"]
    assert build(rsSpy20=0.0)["uptrend"] is False, "flat RS is not an uptrend"


def test_uptrend_ignores_rs_spy_5():
    """rsSpy5 changes, nothing else does → the same reads (item 9)."""
    assert build(rsSpy5=-9.0) == build(rsSpy5=40.0)


def test_reads_ignore_session_so_far():
    """The session view is context for the model, never an input here."""
    assert build() == build(sessionSoFar=None) == build(sessionSoFar__last=1.0)


# ── Missing inputs, purity ───────────────────────────────────────

@pytest.mark.parametrize("path, value, gone", [
    ("volumeRead", None, {"lowVolumeBreakout", "distribution", "dryPullback"}),
    ("momentumRead", None, {"bleeding"}),                     # `dead` was not firing (move −2.0)
    ("rangeRead", None, {"rangeBound"}),
    ("volumeRead__breakout", None, {"lowVolumeBreakout"}),     # no breakout: the flag is false, so not raised
    ("volumeRead__upDays5Rvol", None, {"distribution"}),
    ("momentumRead__closesBelowEma20", None, {"bleeding"}),
    ("rangeRead__posFrac", None, {"rangeBound"}),
])
def test_flag_absent_when_input_null(path, value, gone):
    """A flag whose input is null is neither raised nor withheld (a pre-part
    dossier body, a short history)."""
    firing = dict(volumeRead__breakout={"date": "d", "low": 1, "high": 2, "barRvol": 0.5},
                  volumeRead__downDays5Rvol=3.0, volumeRead__pullbackDays=3, volumeRead__pullbackRvol=0.3,
                  momentumRead__move30Atr=-2.0, momentumRead__range30Atr=3.0,
                  momentumRead__closesBelowEma20=12, momentumRead__lowerHighs=True,
                  rangeRead__posFrac=0.5, rangeRead__ema20Crosses40=8)
    everything = build(**firing)
    assert set(everything["flags"]) == {"lowVolumeBreakout", "distribution", "dryPullback", "bleeding", "rangeBound"}
    section = indicators(**firing)
    block, _, key = path.partition("__")
    if key:
        section[block][key] = value
    else:
        section[block] = value
    out = reads.build(section, today=TODAY, now=AFTER_CLOSE)
    assert set(everything["flags"]) - set(out["flags"]) == gone and out["withheld"] == []
    # An old body with none of the four blocks: no flag, and the uptrend fails with its reasons
    bare = reads.build({"close": 1.0, "rsSpy20": 2.0}, today=TODAY, now=AFTER_CLOSE)
    assert bare["flags"] == [] and bare["withheld"] == [] and bare["uptrend"] is False
    assert bare["trendReasons"][0].endswith("✗") and bare["trendReasons"][3] == "higher swing lows ✗ (unknown)"


def test_reads_deterministic_and_pure():
    section = indicators(volumeRead__breakout={"date": "d", "low": 1, "high": 2, "barRvol": 0.5})
    before = copy.deepcopy(section)
    a = reads.build(section, today=TODAY, now=AFTER_CLOSE)
    b = reads.build(section, today=TODAY, now=AFTER_CLOSE)
    assert a == b and section == before
    assert reads.build("not a dict", today=TODAY, now=AFTER_CLOSE)["flags"] == []


# ── The partial bar ──────────────────────────────────────────────

def test_partial_bar_withholds_volume_flags():
    """The last bar is today's open session: the three volume flags are
    withheld and named, the close / range flags stay (spec 4.8b decision 5)."""
    firing = dict(asOf="2026-09-21T00:00:00Z",
                  volumeRead__breakout={"date": "d", "low": 1, "high": 2, "barRvol": 0.5},
                  volumeRead__downDays5Rvol=3.0, momentumRead__move30Atr=0.1, momentumRead__range30Atr=2.0)
    live = reads.build(indicators(**firing), today=TODAY, now=IN_SESSION)
    assert live["lastBarPartial"] is True and live["flags"] == ["dead"]
    assert live["withheld"] == ["lowVolumeBreakout", "distribution", "dryPullback"]
    closed = reads.build(indicators(**firing), today=TODAY, now=AFTER_CLOSE)
    assert closed["lastBarPartial"] is False and closed["withheld"] == []
    assert closed["flags"] == ["lowVolumeBreakout", "distribution", "dead"]
    # yesterday's bar read in today's session is a closed bar
    older = reads.build(indicators(**{**firing, "asOf": "2026-09-18T00:00:00Z"}), today=TODAY, now=IN_SESSION)
    assert older["lastBarPartial"] is False and older["flags"] == closed["flags"]
    # a weekend "asOf" or an unreadable one is never partial
    assert reads.last_bar_partial("2026-09-20", date(2026, 9, 20), IN_SESSION) is False
    assert reads.last_bar_partial(None, TODAY, IN_SESSION) is False
    # a flag whose input is null stays absent, partial or not
    partial_null = reads.build(indicators(asOf="2026-09-21T00:00:00Z", volumeRead=None), today=TODAY, now=IN_SESSION)
    assert partial_null["withheld"] == [] and partial_null["lastBarPartial"] is True


# ── The eleven (spec 4.8b-ai decision 2) ─────────────────────────

# verdict → (uptrend, stackUp, ema20Rising10, rsSpy20, higherLows,
#            move30Atr, range30Atr, closesBelowEma20, lowerHighs,
#            posFrac, ema20Crosses40, closedOutside, flags)
ELEVEN = {
    "3c31ff2c": (False, True, True, None, True, 3.42, 5.16, 4, False, 0.93, 6, False, []),
    "becd874d": (False, True, True, None, True, 3.42, 5.16, 4, False, 0.93, 6, False, []),
    "8dee4d5b": (False, False, True, None, True, 0.11, 3.91, 12, False, 0.58, 7, False, ["dead", "rangeBound"]),
    "6c1da5ac": (False, False, False, -2.0477, False, -2.72, 10.12, 19, False, 0.22, 3, False, []),
    "20824f75": (False, False, False, -3.0759, False, -5.60, 7.59, 19, True, 0.16, 3, False,
                 ["lowVolumeBreakout", "bleeding"]),
    "2baca035": (False, True, True, -6.0446, False, 2.17, 5.81, 4, True, 0.75, 5, False, ["rangeBound"]),
    "3d4ebf19": (False, True, True, -0.212, False, 0.24, 2.81, 5, False, 0.76, 6, False, ["dead", "rangeBound"]),
    "f70e5368": (False, True, False, -1.9843, False, -0.87, 4.84, 14, False, 0.75, 4, False, ["dead"]),
    "e62eff60": (True, True, True, 2.5345, True, 0.25, 4.11, 1, True, 0.90, 3, False, ["dead"]),
    "96bea108": (True, True, True, 20.6696, True, 2.47, 5.19, 7, False, 0.58, 10, False, ["rangeBound"]),
    "35ddee39": (False, False, False, -2.0477, False, -0.93, 9.69, 18, False, 0.29, 3, False, []),
}
# the strict breakout rule: 5 of 11, the sub-1.0 bar on AAL only
BREAKOUTS = {"8dee4d5b": ("2026-09-18", 344.46, 348.32, 1.99), "20824f75": ("2026-09-18", 12.79, 12.95, 0.74),
             "3d4ebf19": ("2026-09-16", 23.83, 24.16, 2.23), "e62eff60": ("2026-09-21", 489.20, 493.81, 1.33),
             "96bea108": ("2026-09-21", 23.49, 23.93, 1.40)}


def _fixture():
    rows = json.loads(FIXTURE.read_text())
    assert [r["verdict"] for r in rows] == list(ELEVEN)
    return {r["verdict"]: r for r in rows}


@pytest.mark.parametrize("verdict", list(ELEVEN))
def test_flags_on_the_eleven(verdict):
    row = _fixture()[verdict]
    up, stack, rising, rs, higher, move, span, below, lower, pos, crosses, outside, flags = ELEVEN[verdict]
    assert row["rsSpy20"] == rs, "the stored rsSpy20Pct (read-only prod query, 2026-09-25)"
    t, m, r, v = row["trendRead"], row["momentumRead"], row["rangeRead"], row["volumeRead"]
    assert (t["stackUp"], t["ema20Rising10"], t["higherLows"]) == (stack, rising, higher)
    assert (round(m["move30Atr"], 2), round(m["range30Atr"], 2), m["closesBelowEma20"], m["lowerHighs"]) == (
        move, span, below, lower)
    assert (round(r["posFrac"], 2), r["ema20Crosses40"], r["closedOutside"]) == (pos, crosses, outside)
    b = v["breakout"]
    got = None if b is None else (b["date"], round(b["low"], 2), round(b["high"], 2), round(b["barRvol"], 2))
    assert got == BREAKOUTS.get(verdict)

    section = {"asOf": f"{row['asOf']}T00:00:00Z", "rsSpy20": row["rsSpy20"], "volumeRead": v,
               "trendRead": t, "momentumRead": m, "rangeRead": r}
    as_of = date.fromisoformat(row["asOf"])
    out = reads.build(section, today=as_of, now=datetime.combine(as_of, time(17, 0), ET))
    assert (out["uptrend"], out["flags"], out["withheld"], out["lastBarPartial"]) == (up, flags, [], False)


def test_eleven_totals():
    counts = {}
    for _, *_, flags in ELEVEN.values():
        for f in flags:
            counts[f] = counts.get(f, 0) + 1
    assert counts == {"dead": 4, "rangeBound": 4, "bleeding": 1, "lowVolumeBreakout": 1}
    assert len(BREAKOUTS) == 5 and sum(1 for v in ELEVEN.values() if v[0]) == 2
    assert [k for k, v in ELEVEN.items() if not v[-1]] == ["3c31ff2c", "becd874d", "6c1da5ac", "35ddee39"]
