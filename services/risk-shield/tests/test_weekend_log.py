"""Part 3.4c commit 4 — the weekend log (spec D11, Change 3, F13).
Pure pairing over fake rows; the route over db.py's real helpers on a fake
pool. No socket."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import db
import main
import weekend_log

ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc)


CLOSE = et(2026, 9, 18, 16, 0)


def block(level, points=2, reasons=("vix_high",), close_at=CLOSE, gap=65.5):
    return {"version": 1, "level": level, "points": points,
            "reasons": [{"code": c, "detail": "", "weight": 2} for c in reasons],
            "inputs": {"gapHours": gap, "closeAt": close_at.isoformat(),
                       "nextOpenAt": et(2026, 9, 21, 9, 30).isoformat()},
            "assessedAt": close_at.isoformat()}


def fut(es, nq=None):
    out = {}
    for ticker, price in (("ES=F", es), ("NQ=F", nq)):
        out[ticker] = None if price is None else {"price": price, "date": "2026-09-18",
                                                  "asOf": CLOSE.isoformat(), "stale": False}
    return out


def wrow(at, kind, level, *, futures=None, points=2, reasons=("vix_high",), close_at=CLOSE):
    return {"checkedAt": at, "score": 58, "regime": "CAUTIOUS", "kind": kind,
            "weekend": block(level, points, reasons, close_at),
            "futures": futures if futures is not None else fut(5000.0, 20000.0)}


def mrow(at, *, es=5000.0, nq=20000.0, score=60, regime="CAUTIOUS"):
    return {"checkedAt": at, "score": score, "regime": regime, "futures": fut(es, nq)}


FRIDAY_ROWS = [
    wrow(et(2026, 9, 18, 15, 30), "market", "ELEVATED"),
    wrow(et(2026, 9, 18, 15, 55), "market", "HIGH", points=4, reasons=("vix_high", "scheduled_event")),
    wrow(et(2026, 9, 18, 16, 0), "market", "LOW"),            # at the bell: never the close level
    wrow(et(2026, 9, 18, 16, 20), "settle", "ELEVATED"),
]
MONDAY_OPEN = mrow(et(2026, 9, 21, 9, 30), es=4900.0, nq=19400.0, score=44, regime="DANGER")


# ── The pairing (Change 3) ───────────────────────────────────────

def test_level_at_close_is_the_last_row_before_the_bell():
    [row] = weekend_log.build_rows(FRIDAY_ROWS, [MONDAY_OPEN])
    assert row["levelAtClose"] == "HIGH"          # 15:55, not the 16:00 row
    assert row["levelAtSettle"] == "ELEVATED"
    assert row["points"] == 4 and row["reasons"] == ["vix_high", "scheduled_event"]


def test_move_is_measured_from_the_settle_to_the_next_open():
    [row] = weekend_log.build_rows(FRIDAY_ROWS, [MONDAY_OPEN])
    assert row["esMovePct"] == pytest.approx(-2.0)
    assert row["nqMovePct"] == pytest.approx(-3.0)
    assert row["status"] == weekend_log.STATUS_SCORED
    assert (row["nextOpenScore"], row["nextOpenRegime"]) == (44, "DANGER")
    assert row["closeDate"] == "2026-09-18" and row["gapHours"] == 65.5


def test_log_pending_when_no_next_session():
    """F13: the weekend just closed; never dropped, never guessed."""
    [row] = weekend_log.build_rows(FRIDAY_ROWS, [])
    assert row["status"] == weekend_log.STATUS_PENDING
    assert row["esMovePct"] is None and row["nextOpenAt"] is None
    assert row["levelAtClose"] == "HIGH"          # the level is still reported


def test_unscored_when_the_settle_stored_no_futures():
    rows = [r for r in FRIDAY_ROWS if r["kind"] != "settle"]
    rows.append(wrow(et(2026, 9, 18, 16, 20), "settle", "ELEVATED", futures=fut(None, None)))
    [row] = weekend_log.build_rows(rows, [MONDAY_OPEN])
    assert row["status"] == weekend_log.STATUS_UNSCORED and row["esMovePct"] is None


def test_a_missing_close_row_falls_back_to_the_settle():
    [row] = weekend_log.build_rows([FRIDAY_ROWS[3]], [MONDAY_OPEN])
    assert row["levelAtClose"] is None and row["levelAtSettle"] == "ELEVATED"
    assert row["points"] == 2 and row["status"] == weekend_log.STATUS_SCORED


def test_rows_are_newest_first_and_one_per_session():
    earlier = [wrow(et(2026, 9, 11, 15, 55), "market", "LOW", close_at=et(2026, 9, 11, 16, 0)),
               wrow(et(2026, 9, 11, 16, 20), "settle", "LOW", close_at=et(2026, 9, 11, 16, 0))]
    rows = weekend_log.build_rows(earlier + FRIDAY_ROWS,
                                  [mrow(et(2026, 9, 14, 9, 30)), MONDAY_OPEN])
    assert [r["closeDate"] for r in rows] == ["2026-09-18", "2026-09-11"]


def test_rows_without_a_block_are_ignored():
    junk = [{"checkedAt": et(2026, 9, 18, 15, 55), "score": 1, "regime": "X", "kind": "market",
             "weekend": {"level": "NOPE"}, "futures": {}}]
    assert weekend_log.build_rows(junk, [MONDAY_OPEN]) == []


def test_next_session_row_is_the_first_after_the_settle():
    same_day = mrow(et(2026, 9, 18, 15, 30))
    assert weekend_log.next_session_row([same_day, MONDAY_OPEN],
                                        et(2026, 9, 18, 16, 20)) is MONDAY_OPEN


# ── The summary (D11) ────────────────────────────────────────────

def _weekend(level_close, level_settle, es, status=weekend_log.STATUS_SCORED):
    return {"closeDate": "2026-09-18", "levelAtClose": level_close, "levelAtSettle": level_settle,
            "points": 4, "reasons": [], "gapHours": 65.5, "esMovePct": es, "nqMovePct": es,
            "nextOpenAt": None, "nextOpenScore": None, "nextOpenRegime": None, "status": status}


def test_summary_grades_the_actionable_level():
    """Change 3: the question is whether HIGH-at-15:55 predicted a bad
    Monday, so the summary runs on levelAtClose, not on the settle's."""
    rows = [_weekend("HIGH", "LOW", -3.0), _weekend("HIGH", "HIGH", -1.0),
            _weekend("LOW", "HIGH", 0.5)]
    summary = weekend_log.summarize(rows)
    assert summary["levels"]["HIGH"] == {"n": 2, "meanEsMovePct": -2.0, "worstEsMovePct": -3.0}
    assert summary["levels"]["LOW"] == {"n": 1, "meanEsMovePct": 0.5, "worstEsMovePct": 0.5}
    assert summary["levels"]["ELEVATED"]["n"] == 0
    assert summary["levels"]["ELEVATED"]["meanEsMovePct"] is None


def test_summary_counts_statuses_and_disagreements():
    rows = [_weekend("HIGH", "LOW", -3.0),
            _weekend("LOW", "LOW", None, weekend_log.STATUS_PENDING),
            _weekend("ELEVATED", "ELEVATED", None, weekend_log.STATUS_UNSCORED)]
    summary = weekend_log.summarize(rows)
    assert (summary["weekends"], summary["scored"], summary["pending"],
            summary["unscored"]) == (3, 1, 1, 1)
    assert summary["disagreements"] == 1


def test_summary_ignores_unscored_moves():
    rows = [_weekend("HIGH", "HIGH", None, weekend_log.STATUS_PENDING)]
    assert weekend_log.summarize(rows)["levels"]["HIGH"]["n"] == 0


# ── The route ────────────────────────────────────────────────────

class LogPool:
    """Answers the two log reads by which SQL arrived."""

    def __init__(self, weekend_rows=(), first_rows=(), fail=None):
        self.weekend_rows, self.first_rows, self.fail = list(weekend_rows), list(first_rows), fail
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.fail:
            raise self.fail
        return self.weekend_rows if "jsonb_typeof" in sql else self.first_rows

    async def execute(self, *args):
        raise AssertionError("the log never writes")


def db_row(row, *, kind=None):
    """What asyncpg hands db.py: JSONB columns as text."""
    out = {"checked_at": row["checkedAt"], "score": row["score"], "regime": row["regime"],
           "futures": json.dumps(row["futures"])}
    if kind is not None:
        out["kind"] = kind
        out["weekend"] = json.dumps(row["weekend"])
    return out


@pytest.fixture
def client_with(monkeypatch):
    def _make(pool, now=et(2026, 9, 21, 12, 0)):
        monkeypatch.setattr(main, "_now", lambda: now)
        main.app.state.db_pool = pool
        main.app.state.redis = None
        return TestClient(main.app)
    yield _make
    main.app.state.db_pool = None


def test_log_route_serves_rows_and_summary(client_with):
    pool = LogPool([db_row(r, kind=r["kind"]) for r in FRIDAY_ROWS], [db_row(MONDAY_OPEN)])
    body = client_with(pool).get("/market/weekend/log").json()
    assert body["weeks"] == 26
    [row] = body["rows"]
    assert row["levelAtClose"] == "HIGH" and row["levelAtSettle"] == "ELEVATED"
    assert row["esMovePct"] == pytest.approx(-2.0)
    assert body["summary"]["levels"]["HIGH"]["n"] == 1
    assert body["summary"]["disagreements"] == 1
    assert len(pool.calls) == 2                       # exactly two reads


def test_log_route_window_is_bounded(client_with):
    client = client_with(LogPool())
    assert client.get("/market/weekend/log?weeks=52").json()["weeks"] == 52
    assert client.get("/market/weekend/log?weeks=0").status_code == 422
    assert client.get("/market/weekend/log?weeks=53").status_code == 422
    since = datetime.fromisoformat(client.get("/market/weekend/log?weeks=1").json()["since"])
    assert since == et(2026, 9, 21, 12, 0) - timedelta(weeks=1)


def test_log_route_empty_history(client_with):
    body = client_with(LogPool()).get("/market/weekend/log").json()
    assert body["rows"] == [] and body["summary"]["weekends"] == 0
    assert body["summary"]["levels"]["HIGH"]["n"] == 0


def test_log_route_503_without_a_pool(client_with):
    client = client_with(LogPool())
    main.app.state.db_pool = None
    assert client.get("/market/weekend/log").status_code == 503


def test_log_route_503_on_a_database_failure(client_with):
    import asyncpg
    assert client_with(LogPool(fail=asyncpg.PostgresError("boom"))).get(
        "/market/weekend/log").status_code == 503


def test_read_helpers_ask_for_bounded_sets():
    """The SQL keeps both reads small: blocks only, one market row per date."""
    assert "jsonb_typeof(indicators->'weekend') = 'object'" in db.WEEKEND_ROWS_SQL
    assert "DISTINCT ON" in db.FIRST_MARKET_ROW_SQL
    assert "America/New_York" in db.FIRST_MARKET_ROW_SQL
    for sql in (db.WEEKEND_ROWS_SQL, db.FIRST_MARKET_ROW_SQL):
        assert "checked_at >= $1" in sql and "monitors" not in sql
