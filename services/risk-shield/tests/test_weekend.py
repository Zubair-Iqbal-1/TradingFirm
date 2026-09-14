"""Part 3.4c — the weekend-exposure signal (spec D1, D6, D8, D10, F12).
Pure: no clock, no I/O. The level table and the phrase list are pinned here,
because the spec's tables are the contract the numbers are judged against."""

import pytest

from scoring import weekend

AS_OF = "2026-09-18T19:35:02+00:00"        # Friday 15:35 ET, inside the session
ASSESSED = "2026-09-18T19:35:02+00:00"
SETTLE_AS_OF = "2026-09-18T20:20:02+00:00"  # Friday 16:20 ET, after the 16:15 cut


def vix_view(closes, dates=None, as_of=AS_OF, stale=False, source="fresh"):
    """A quotes view carrying only what vix5d reads."""
    dates = dates or [f"2026-09-{d:02d}" for d in (10, 11, 14, 15, 16, 17, 18)][-len(closes):]
    return {"asOf": as_of, "source": source, "reason": None,
            "staleTickers": ["^VIX"] if stale else [],
            "tickers": {"^VIX": {"date": dates, "close": list(closes),
                                 "asOf": as_of, "stale": stale}}}


def events(*titles, status=weekend.EVENTS_OK, coverage_short=False):
    return {"status": status, "coverageShort": coverage_short,
            "events": [{"date": "2026-09-20", "time": "14:00", "type": "event",
                        "title": t, "detail": ""} for t in titles]}


def news(*texts, status="ok", hours=24):
    return {"status": status, "hours": hours,
            "items": [{"publishedAt": "2026-09-18T17:00:00+00:00", "source": "Reuters",
                       "title": t, "summary": ""} for t in texts]}


def block(**over):
    """assess() with LOW-by-default inputs, overridden per test."""
    args = dict(capped_score=80, base_score=80, regime="HEALTHY",
                vix=weekend.vix5d(vix_view([14.0] * 7)), events=events(), news=news(),
                situation=None, window={"gapHours": 65, "baseScoreAsOf": "2026-09-17"},
                assessed_at=ASSESSED)
    args.update(over)
    return weekend.assess(**args)


# ── The level table (spec D1) ────────────────────────────────────

def test_levels_pinned_to_spec():
    """The weights, the thresholds and the override are the spec's table."""
    assert weekend.WEIGHTS == {
        "regime_weak": 2, "regime_soft": 1,
        "vix_high": 2, "vix_elevated": 1, "vix_rising": 1,
        "scheduled_event": 2, "pending_decision": 1, "active_situation": 2,
        "calendar_unavailable": 0,
    }
    assert (weekend.HIGH_POINTS, weekend.ELEVATED_POINTS) == (4, 2)
    assert (weekend.REGIME_WEAK_MAX, weekend.REGIME_SOFT_MAX) == (39, 59)
    assert (weekend.VIX_HIGH_LEVEL, weekend.VIX_ELEVATED_LEVEL) == (25.0, 20.0)
    assert (weekend.VIX_RISING_PCT, weekend.VIX_SESSIONS, weekend.PENDING_MANY) == (15.0, 5, 3)


@pytest.mark.parametrize("points, level", [
    (0, weekend.LEVEL_LOW), (1, weekend.LEVEL_LOW),
    (2, weekend.LEVEL_ELEVATED), (3, weekend.LEVEL_ELEVATED),
    (4, weekend.LEVEL_HIGH), (7, weekend.LEVEL_HIGH),
])
def test_level_thresholds_are_inclusive(points, level):
    reasons = [{"code": f"r{i}", "detail": "", "weight": 1} for i in range(points)]
    assert weekend.level_for(reasons) == (level, points)


def test_active_situation_alone_is_never_high():
    alone = [{"code": weekend.REASON_ACTIVE_SITUATION, "detail": "", "weight": 2}]
    assert weekend.level_for(alone) == (weekend.LEVEL_ELEVATED, 2)
    with_other = alone + [{"code": weekend.REASON_VIX_HIGH, "detail": "", "weight": 2}]
    assert weekend.level_for(with_other) == (weekend.LEVEL_HIGH, 4)


def test_zero_weight_note_never_lifts_a_level():
    note = [{"code": weekend.REASON_CALENDAR_UNAVAILABLE, "detail": "", "weight": 0}]
    assert weekend.level_for(note) == (weekend.LEVEL_LOW, 0)


@pytest.mark.parametrize("score, code", [
    (39, weekend.REASON_REGIME_WEAK), (40, weekend.REASON_REGIME_SOFT),
    (59, weekend.REASON_REGIME_SOFT), (60, None),
])
def test_regime_reason_bands_use_the_capped_score(score, code):
    """Spec Change 2: the reason reads the published (capped) score, and the
    block records that it did. baseScore rides along untouched."""
    b = block(capped_score=score, base_score=95)
    codes = [r["code"] for r in b["reasons"]]
    assert (code in codes) if code else not any(c.startswith("regime_") for c in codes)
    assert b["inputs"]["cappedScore"] == score and b["inputs"]["baseScore"] == 95
    assert b["inputs"]["regimeSource"] == "capped"


@pytest.mark.parametrize("level, code", [
    (19.99, None), (20.0, weekend.REASON_VIX_ELEVATED), (24.99, weekend.REASON_VIX_ELEVATED),
    (25.0, weekend.REASON_VIX_HIGH), (40.0, weekend.REASON_VIX_HIGH),
])
def test_vix_band_edges(level, code):
    b = block(vix=weekend.vix5d(vix_view([14.0] * 6 + [level])))
    codes = [r["code"] for r in b["reasons"]]
    assert (code in codes) if code else not any(c.startswith("vix_") for c in codes)


def test_high_needs_two_independent_reasons():
    """VIX 26 alone is ELEVATED; VIX 26 with a scheduled event is HIGH."""
    hot = weekend.vix5d(vix_view([14.0] * 6 + [26.0]))
    assert block(vix=hot)["level"] == weekend.LEVEL_ELEVATED
    both = block(vix=hot, events=events("EU tariff deadline"))
    assert both["level"] == weekend.LEVEL_HIGH and both["points"] == 4


def test_many_events_are_capped_at_one_reason_worth_two():
    b = block(events=events("summit", "vote", "deadline"))
    fired = [r for r in b["reasons"] if r["code"] == weekend.REASON_SCHEDULED_EVENT]
    assert len(fired) == 1 and fired[0]["weight"] == 2 and b["points"] == 2


def test_three_matching_headlines_weigh_two():
    one = block(news=news("Talks this weekend on the border"))
    assert [r["weight"] for r in one["reasons"] if r["code"] == weekend.REASON_PENDING_DECISION] == [1]
    three = block(news=news("Talks this weekend", "Decision expected", "Tariff deadline nears"))
    assert [r["weight"] for r in three["reasons"] if r["code"] == weekend.REASON_PENDING_DECISION] == [2]


def test_reasons_are_weight_ordered_and_capped():
    b = block(capped_score=30, vix=weekend.vix5d(vix_view([14.0] * 6 + [30.0])),
              events=events("summit"), news=news("Decision expected"),
              situation={"text": "port strike", "setAt": ASSESSED, "expiresAt": "2026-09-30T00:00:00+00:00"})
    weights = [r["weight"] for r in b["reasons"]]
    assert weights == sorted(weights, reverse=True)
    assert len(b["reasons"]) <= weekend.MAX_REASONS
    assert b["level"] == weekend.LEVEL_HIGH


# ── The VIX snapshot (spec D10, Change 1) ────────────────────────

def test_vix_level_is_the_partial_bar_but_direction_is_complete_bars():
    """The Friday-afternoon read is live: level comes from today's partial
    bar, while the 5-day direction stays on complete closes."""
    snap = weekend.vix5d(vix_view([15.0, 15.0, 15.0, 15.0, 15.0, 16.0, 22.0]))
    assert snap["partial"] is True and snap["level"] == 22.0
    assert snap["closes"][-1] == 16.0 and snap["prevClose"] == 16.0
    change, direction = weekend.vix_direction(snap)
    assert direction == weekend.DIRECTION_RISING and change == pytest.approx(6.666, abs=0.01)


def test_vix_snapshot_after_the_settle_has_no_partial_bar():
    snap = weekend.vix5d(vix_view([15.0] * 6 + [16.0], as_of=SETTLE_AS_OF))
    assert snap["partial"] is False and snap["level"] == 16.0
    assert snap["closes"][-1] == 16.0 and snap["prevClose"] == 15.0


@pytest.mark.parametrize("closes, direction", [
    ([10.0, 10.4], weekend.DIRECTION_FLAT),        # +4.0 %
    ([10.0, 10.5], weekend.DIRECTION_RISING),      # +5.0 %, the band is inclusive
    ([10.0, 9.6], weekend.DIRECTION_FLAT),         # −4.0 %
    ([10.0, 9.5], weekend.DIRECTION_FALLING),      # −5.0 %
    ([10.0, 15.0], weekend.DIRECTION_RISING),
])
def test_vix_direction_bands(closes, direction):
    complete = [closes[0]] + [closes[-1]] * 5      # 6 complete closes: first vs last
    snap = weekend.vix5d(vix_view(complete, as_of=SETTLE_AS_OF))
    assert weekend.vix_direction(snap)[1] == direction


def test_vix_rising_reason_needs_fifteen_percent():
    at_15 = weekend.vix5d(vix_view([10.0, 11.5, 11.5, 11.5, 11.5, 11.5], as_of=SETTLE_AS_OF))
    assert weekend.vix_direction(at_15)[0] == pytest.approx(15.0)
    assert weekend.REASON_VIX_RISING in [r["code"] for r in block(vix=at_15)["reasons"]]
    below = weekend.vix5d(vix_view([10.0, 11.4, 11.4, 11.4, 11.4, 11.4], as_of=SETTLE_AS_OF))
    assert weekend.REASON_VIX_RISING not in [r["code"] for r in block(vix=below)["reasons"]]


@pytest.mark.parametrize("view, why", [
    ({"tickers": {}, "staleTickers": [], "source": "fresh", "asOf": AS_OF}, "missing"),
    (vix_view([14.0] * 7, stale=True), "stale"),
    (vix_view([]), "no bars"),
])
def test_vix_missing_no_direction_reason(view, why):
    """F7: no usable ^VIX → no vix inputs, direction unknown, no VIX reason,
    and the level is still computed from everything else."""
    snap = weekend.vix5d(view)
    assert snap is None, why
    b = block(vix=snap, events=events("summit"))
    assert b["inputs"]["vixLevel"] is None
    assert b["inputs"]["vixDirection"] == weekend.DIRECTION_UNKNOWN
    assert not any(r["code"].startswith("vix_") for r in b["reasons"])
    assert b["level"] == weekend.LEVEL_ELEVATED


def test_vix_direction_unknown_with_too_few_complete_closes():
    snap = weekend.vix5d(vix_view([15.0, 15.5, 16.0], as_of=SETTLE_AS_OF))
    assert len(snap["closes"]) < weekend.VIX_SESSIONS + 1
    assert weekend.vix_direction(snap) == (None, weekend.DIRECTION_UNKNOWN)


# ── Pending-decision language (spec D6) ──────────────────────────

def test_phrases_pinned_to_spec():
    assert weekend.PHRASES == (
        r"expected to announce",
        r"deadline (?:sunday|saturday|this weekend)",
        r"talks (?:this |over the )?weekend",
        r"emergency (?:meeting|session|summit)",
        r"decision (?:is )?expected",
        r"vote (?:on )?(?:sunday|saturday)",
        r"ceasefire (?:deadline|talks)",
        r"tariff deadline",
        r"summit",
        r"ahead of monday",
    )


@pytest.mark.parametrize("text, hit", [
    ("Officials are EXPECTED TO ANNOUNCE a package", True),
    ("Deadline Sunday for the trade pact", True),
    ("Ceasefire talks resume", True),
    ("Leaders hold a summit", True),
    ("Quarterly results beat estimates", False),
    ("The summitry of the 1980s", False),          # word boundary, not a substring
])
def test_phrase_matching_is_case_insensitive_and_bounded(text, hit):
    assert bool(weekend.match_phrases(news(text)["items"])) is hit


def test_match_records_the_phrase_and_caps_the_list():
    matched = weekend.match_phrases(news(*[f"Summit number {i}" for i in range(9)])["items"])
    assert len(matched) == weekend.MAX_NEWS
    assert matched[0]["phrase"] == "summit" and matched[0]["source"] == "Reuters"


def test_match_skips_junk_items_without_raising():
    assert weekend.match_phrases([None, 5, {"title": None, "summary": None}]) == []


def test_news_summary_is_searched_not_only_the_title():
    items = [{"publishedAt": AS_OF, "source": "AP", "title": "Trade",
              "summary": "A decision is expected before Monday"}]
    assert weekend.match_phrases(items)[0]["phrase"] == "decision (?:is )?expected"


# ── The active situation (spec D2, F14) ──────────────────────────

def test_expired_situation_ignored():
    live = {"text": "port strike", "setAt": ASSESSED, "expiresAt": "2026-09-30T00:00:00+00:00"}
    gone = {"text": "port strike", "setAt": ASSESSED, "expiresAt": "2026-09-18T00:00:00+00:00"}
    assert weekend.situation_active(live, ASSESSED)["text"] == "port strike"
    assert weekend.situation_active(gone, ASSESSED) is None
    assert block(situation=gone)["inputs"]["activeSituation"] is None
    assert not any(r["code"] == weekend.REASON_ACTIVE_SITUATION for r in block(situation=gone)["reasons"])


@pytest.mark.parametrize("value", [None, {}, {"text": "  "}, {"text": 5}, "a string"])
def test_situation_wrong_shapes_read_as_absent(value):
    assert weekend.situation_active(value, ASSESSED) is None


def test_situation_text_is_capped():
    long = {"text": "x" * 500, "setAt": ASSESSED, "expiresAt": None}
    assert len(weekend.situation_active(long, ASSESSED)["text"]) == weekend.SITUATION_TEXT_MAX


# ── The non-finite guard (spec F12, Change 4) ────────────────────

@pytest.mark.parametrize("value, path", [
    ({"inputs": {"vixLevel": float("nan")}}, "weekend.inputs.vixLevel"),
    ({"reasons": [{"weight": float("inf")}]}, "weekend.reasons[0].weight"),
    ({"points": float("-inf")}, "weekend.points"),
])
def test_nonfinite_path_finds_it_anywhere(value, path):
    assert weekend.nonfinite_path(value) == path


def test_nonfinite_path_none_on_a_clean_block():
    assert weekend.nonfinite_path(block()) is None


def test_nan_drops_block_not_publish(caplog):
    """Change 4: the block is dropped before it can reach json.dumps, so it
    can never fail a publish. One ERROR per process, then DEBUG."""
    weekend._nan_logged = False
    dirty = block()
    dirty["inputs"]["vix5dChangePct"] = float("nan")
    with caplog.at_level("DEBUG"):
        first, dropped = weekend.drop_if_nonfinite(dirty)
        second, dropped_again = weekend.drop_if_nonfinite(dirty)
    assert (first, dropped) == (None, "nan") and (second, dropped_again) == (None, "nan")
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "weekend.inputs.vix5dChangePct" in errors[0].message
    assert sum(r.levelname == "DEBUG" for r in caplog.records) == 1


def test_clean_block_passes_the_guard_untouched():
    clean = block()
    assert weekend.drop_if_nonfinite(clean) == (clean, None)


def test_drop_if_nonfinite_accepts_no_block():
    assert weekend.drop_if_nonfinite(None) == (None, None)


# ── The block (spec D8, F6, F15) ─────────────────────────────────

def test_no_score_no_block():
    """F6: a level with no score would be a guess."""
    assert block(capped_score=None) is None


def test_block_shape_is_the_spec_table():
    b = block(events=events("EU tariff deadline", coverage_short=True))
    assert set(b) == {"version", "level", "points", "reasons", "inputs", "assessedAt"}
    assert b["version"] == weekend.VERSION and b["assessedAt"] == ASSESSED
    assert set(b["inputs"]) == {
        "regime", "cappedScore", "baseScore", "regimeSource", "baseScoreAsOf",
        "vixLevel", "vixPrevClose", "vix5dChangePct", "vixDirection",
        "vixAsOf", "vixPartial", "quotesSource",
        "gapHours", "closeAt", "nextOpenAt", "events", "news", "activeSituation"}
    assert b["inputs"]["events"]["coverageShort"] is True
    assert b["inputs"]["gapHours"] == 65 and b["inputs"]["baseScoreAsOf"] == "2026-09-17"
    assert b["inputs"]["vixPartial"] is True and b["inputs"]["quotesSource"] == "fresh"
    assert b["level"] in weekend.LEVELS


def test_block_is_idempotent():
    """F15: a restart inside a slot re-runs the assessment; same inputs in,
    same block out, nothing accumulated."""
    assert block() == block()
    hot = dict(capped_score=30, events=events("summit"))
    assert block(**hot) == block(**hot)


def test_unavailable_calendar_is_a_note_not_a_reason():
    """F1: the level is computed from the rest, and the row says events are
    unknown rather than pretending there were none."""
    b = block(events={"status": weekend.EVENTS_UNAVAILABLE, "coverageShort": False, "events": []})
    codes = [r["code"] for r in b["reasons"]]
    assert weekend.REASON_CALENDAR_UNAVAILABLE in codes
    assert b["points"] == 0 and b["level"] == weekend.LEVEL_LOW
    assert b["inputs"]["events"]["status"] == weekend.EVENTS_UNAVAILABLE


def test_unavailable_news_adds_no_pending_reason():
    """F3: an unreachable data-engine costs the news reason, nothing else."""
    b = block(news={"status": "unavailable", "hours": 24, "items": []}, events=events("summit"))
    assert not any(r["code"] == weekend.REASON_PENDING_DECISION for r in b["reasons"])
    assert b["inputs"]["news"]["status"] == "unavailable" and b["level"] == weekend.LEVEL_ELEVATED


def test_block_carries_no_trade_action():
    """The spec's hard line: a risk report, never a trade action."""
    text = repr(block(capped_score=20, events=events("summit"), news=news("Summit Sunday"))).lower()
    for word in ("flatten", "sell", "buy", "exit", "position", "stop loss"):
        assert word not in text


def test_missing_events_and_news_sections_are_tolerated():
    b = block(events=None, news=None, window=None)
    assert b["level"] == weekend.LEVEL_LOW
    assert b["inputs"]["gapHours"] is None and b["inputs"]["news"]["status"] == "unavailable"


def test_block_is_json_safe():
    """allow_nan=False is what publish_health uses; the block round-trips."""
    import json
    original = block(capped_score=30, events=events("summit"))
    assert json.loads(json.dumps(original, allow_nan=False)) == original
