"""Part 3.4b — the futures overlay (spec decision 4). Pure: no clock, no I/O."""

import pytest

from scoring import overlay
from scoring.regime_classifier import classify

AS_OF = "2026-09-14T20:15:00+00:00"


def view(es=None, nq=None, stale=()):
    """A quotes view carrying only what the overlay reads."""
    tickers = {}
    for ticker, close in (("ES=F", es), ("NQ=F", nq)):
        if close is None:
            continue
        tickers[ticker] = {"date": ["2026-09-11", "2026-09-14"], "close": [close - 1.0, close],
                           "asOf": AS_OF, "stale": ticker in stale}
    return {"asOf": AS_OF, "source": "fresh", "reason": None,
            "tickers": tickers, "staleTickers": list(stale)}


def ref(es=5000.0, nq=20000.0):
    """A settle row's stored futures block."""
    return {ticker: None if price is None else
            {"price": price, "date": "2026-09-14", "asOf": "2026-09-14T20:20:02+00:00", "stale": False}
            for ticker, price in (("ES=F", es), ("NQ=F", nq))}


@pytest.mark.parametrize("price, move, cap, status", [
    (4926.0, -1.48, None, overlay.STATUS_WITHIN),
    (4925.0, -1.50, 69, overlay.STATUS_APPLIED),
    (4851.0, -2.98, 69, overlay.STATUS_APPLIED),
    (4850.0, -3.00, 39, overlay.STATUS_APPLIED),
    (4751.0, -4.98, 39, overlay.STATUS_APPLIED),
    (4750.0, -5.00, 19, overlay.STATUS_APPLIED),
], ids=["-1.48", "-1.50", "-2.98", "-3.00", "-4.98", "-5.00"])
def test_overlay_cap_bands_edges_inclusive(price, move, cap, status):
    score, record = overlay.apply_overlay(80, ref(), overlay.futures_prices(view(es=price, nq=20000.0)))
    assert record["movePct"] == move          # exact: multiplied before dividing
    assert (record["cap"], record["status"]) == (cap, status)
    assert score == (80 if cap is None else min(80, cap))


def test_overlay_never_raises_score():
    # A base already under the cap keeps its score, and the cap still records.
    score, record = overlay.apply_overlay(30, ref(), overlay.futures_prices(view(es=4850.0, nq=20000.0)))
    assert (score, record["cap"], record["capped"]) == (30, 39, False)
    # A rally caps nothing.
    score, record = overlay.apply_overlay(80, ref(), overlay.futures_prices(view(es=5150.0, nq=20600.0)))
    assert (score, record["status"], record["cap"]) == (80, overlay.STATUS_WITHIN, None)
    # A null score stays null, cap or no cap.
    score, record = overlay.apply_overlay(None, ref(), overlay.futures_prices(view(es=4700.0, nq=20000.0)))
    assert score is None and record["cap"] == 19 and record["capped"] is False


def test_overlay_uses_worse_of_es_nq():
    score, record = overlay.apply_overlay(75, ref(), overlay.futures_prices(view(es=4950.0, nq=19380.0)))
    assert record["esPct"] == pytest.approx(-1.0) and record["nqPct"] == pytest.approx(-3.1)
    assert record["movePct"] == pytest.approx(-3.1) and score == 39
    # One side missing: the other decides.
    score, record = overlay.apply_overlay(75, ref(), overlay.futures_prices(view(nq=19380.0)))
    assert record["esPct"] is None and record["movePct"] == pytest.approx(-3.1) and score == 39


@pytest.mark.parametrize("bad", [0, -5000.0, None, float("nan"), float("inf"), "5000", True],
                         ids=["zero", "negative", "null", "nan", "inf", "string", "bool"])
def test_overlay_rejects_bad_reference(bad):
    reference = {"ES=F": {"price": bad}, "NQ=F": None}
    score, record = overlay.apply_overlay(80, reference, overlay.futures_prices(view(es=4700.0, nq=19000.0)))
    assert score == 80                                   # never an inf, never a raise
    assert record["status"] == overlay.STATUS_NO_REFERENCE
    assert record["esPct"] is None and record["movePct"] is None


def test_overlay_unavailable_and_no_reference():
    prices = overlay.futures_prices(view(es=4900.0, nq=19600.0))
    # A settle row written before 3.4b carries no futures block at all.
    score, record = overlay.apply_overlay(80, None, prices)
    assert (score, record["status"], record["base"]) == (80, overlay.STATUS_NO_REFERENCE, 80)
    # A reference, but nothing fresh to compare with.
    score, record = overlay.apply_overlay(80, ref(), {})
    assert (score, record["status"], record["capped"]) == (80, overlay.STATUS_UNAVAILABLE, False)


def test_futures_prices_from_view_skip_stale():
    prices = overlay.futures_prices(view(es=4900.0, nq=19600.0))
    assert prices["ES=F"] == {"price": 4900.0, "date": "2026-09-14", "asOf": AS_OF, "stale": False}
    # A last-known (stale) ticker is not a price to cap by.
    stale = overlay.futures_prices(view(es=4900.0, nq=19600.0, stale=("ES=F",)))
    assert stale["ES=F"] is None and stale["NQ=F"]["price"] == 19600.0
    # Missing ticker, no closes, a NaN close, a non-positive close.
    assert overlay.futures_prices({"tickers": {}, "staleTickers": []})["ES=F"] is None
    for close in ([], [float("nan")], [0.0]):
        body = {"tickers": {"ES=F": {"date": ["2026-09-14"], "close": close, "asOf": AS_OF}},
                "staleTickers": []}
        assert overlay.futures_prices(body)["ES=F"] is None


def test_overlay_bands_pinned_to_spec():
    assert overlay.CAP_BANDS == ((-5.0, 19), (-3.0, 39), (-1.5, 69))
    assert overlay.FUTURES_TICKERS == ("ES=F", "NQ=F")
    # Each cap is the top of one 3.3 regime, so a band maps to exactly one regime.
    assert [classify(cap) for _, cap in overlay.CAP_BANDS] == ["CRITICAL", "DANGER", "CAUTIOUS"]
    assert [classify(cap + 1) for _, cap in overlay.CAP_BANDS] == ["DANGER", "CAUTIOUS", "HEALTHY"]


def test_overlay_is_pure():
    prices, reference = overlay.futures_prices(view(es=4850.0, nq=19400.0)), ref()
    first = overlay.apply_overlay(80, reference, prices)
    second = overlay.apply_overlay(80, reference, prices)
    assert first == second
    assert reference == ref() and prices == overlay.futures_prices(view(es=4850.0, nq=19400.0))
