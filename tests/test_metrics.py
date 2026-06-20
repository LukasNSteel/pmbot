"""Tests for metrics store."""

import json
import time
from pathlib import Path

from pmbot.metrics import MetricsStore


def test_metrics_daily_report(tmp_path):
    db = tmp_path / "test.db"
    store = MetricsStore(str(db))
    store.record_merge("cid1", 10.0)
    store.record_est_reward(1.5)
    store.record_fill({
        "ts": time.time(), "cid": "cid1", "market": "Test",
        "side": "YES", "token": "y", "price": 0.47, "size": 10,
    })
    store.record_fill({
        "ts": time.time(), "cid": "cid2", "market": "Fee market",
        "side": "NO", "token": "n", "price": 0.24, "size": 40, "fee": 0.96,
    })
    report = store.daily_report()
    store.close()
    assert report["spread_capture_usd"] == 10.0
    assert report["est_rewards_usd"] == 1.5
    assert report["maker_fills"] == 2
    assert report["fees_usd"] == -0.96


def test_recent_fills_and_trades_log(tmp_path):
    db = tmp_path / "test.db"
    log_path = tmp_path / "trades.jsonl"
    store = MetricsStore(str(db), trades_log=str(log_path))
    ts = time.time()
    store.record_fill({
        "ts": ts, "cid": "cid1", "market": "Rain tomorrow?",
        "side": "YES", "token": "y", "price": 0.45, "size": 20,
    })
    store.record_fill({
        "ts": ts + 1, "cid": "cid1", "market": "Rain tomorrow?",
        "side": "NO", "token": "n", "price": 0.52, "size": 20, "taker": True,
    })
    fills = store.recent_fills(limit=10)
    assert len(fills) == 2
    assert fills[0]["taker"] is True
    assert fills[1]["taker"] is False
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["side"] == "YES"
    store.close()


def test_sum_earnings_parses_clob_total_shape():
    # Exact shape returned by GET /rewards/user/total (one row per asset).
    rows = [{
        "date": "2026-06-17T00:00:00Z",
        "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        "maker_address": "0xabc",
        "earnings": 6.539251,
        "asset_rate": 0.999791,
    }]
    assert abs(MetricsStore._sum_earnings(rows) - 6.539251 * 0.999791) < 1e-9
    # Multiple collateral assets sum; {"data": [...]} wrapper also handled.
    multi = {"data": [
        {"earnings": 2.0, "asset_rate": 1.0},
        {"earnings": 3.0, "asset_rate": 0.5},
    ]}
    assert MetricsStore._sum_earnings(multi) == 3.5
    assert MetricsStore._sum_earnings([]) == 0.0
    assert MetricsStore._sum_earnings(None) == 0.0


def test_fetch_realized_rewards_records_total(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))

    class FakeClient:
        def get_total_earnings_for_user_for_day(self, date):
            return [{"earnings": 8.02109, "asset_rate": 0.999601}]

    total = store.fetch_realized_rewards(FakeClient(), date="2026-06-16")
    assert abs(total - 8.02109 * 0.999601) < 1e-9
    report = store.daily_report("2026-06-16")
    store.close()
    assert abs(report["realized_rewards_usd"] - total) < 1e-9


def test_fetch_realized_rewards_keeps_prior_value_on_error(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_realized_reward("2026-06-16", 8.0)

    class BoomClient:
        def get_total_earnings_for_user_for_day(self, date):
            raise RuntimeError("401")

    assert store.fetch_realized_rewards(BoomClient(), date="2026-06-16") == 0.0
    # The transient failure must NOT overwrite the previously recorded value.
    report = store.daily_report("2026-06-16")
    store.close()
    assert report["realized_rewards_usd"] == 8.0


def test_performance_report(tmp_path):
    db = tmp_path / "test.db"
    store = MetricsStore(str(db))
    ts = time.time()
    store.record_fill({
        "ts": ts, "cid": "cid1", "market": "Good market",
        "side": "YES", "token": "y", "price": 0.48, "size": 10, "merged": 10,
    })
    store.record_hedge("cid1", 0.50, 10)
    store.record_markout({
        "ts": ts + 30, "fill_ts": ts, "cid": "cid1", "market": "Good market",
        "horizon": 300, "markout": 0.01,
    })
    store.record_markout({
        "ts": ts + 31, "fill_ts": ts, "cid": "cid1", "market": "Good market",
        "horizon": 30, "markout": 0.005,
    })
    with store._lock:
        minute = int(ts) // 60
        store._conn.execute(
            "INSERT INTO uptime (minute_ts, cid, in_band) VALUES (?,?,?)",
            (minute, "cid1", 1),
        )
        store._conn.execute(
            "INSERT INTO uptime (minute_ts, cid, in_band) VALUES (?,?,?)",
            (minute, "cid1", 0),
        )
        store._conn.commit()
    report = store.performance_report()
    store.close()
    assert len(report["markets"]) == 1
    m = report["markets"][0]
    assert m["maker_fills"] == 1
    assert m["merged_pairs"] == 10
    assert m["hedge_cost_usd"] == 5.0
    assert m["markout_cents"] == 1.0
    assert m["uptime_pct"] == 50.0


def test_reward_totals_all_time_and_24h(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store.record_realized_reward("2026-01-01", 5.0)   # old day
    store.record_realized_reward(today, 2.25)         # today
    store.record_est_reward(3.0)                      # today (est)
    out = store.reward_totals()
    store.close()
    assert abs(out["realized_total"] - 7.25) < 1e-9
    assert abs(out["realized_24h"] - 2.25) < 1e-9
    assert abs(out["est_total"] - 3.0) < 1e-9
    assert abs(out["est_24h"] - 3.0) < 1e-9


def test_reward_sample_records_per_minute_per_market(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_reward_sample("cidA", 0.01)
    store.record_reward_sample("cidB", 0.02)
    store.record_reward_sample("cidA", float("nan"))  # NaN must be dropped
    rows = store._conn.execute(
        "SELECT cid, est_usd FROM reward_samples ORDER BY cid").fetchall()
    store.close()
    assert rows == [("cidA", 0.01), ("cidB", 0.02)]


def test_reward_rate_recent_and_by_market(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    now_min = int(time.time()) // 60
    # 10 minutes of accrual: cidA $0.01/min, cidB $0.02/min.
    with store._lock:
        for i in range(10):
            m = now_min - i
            store._conn.execute(
                "INSERT INTO reward_samples (minute_ts, cid, est_usd) VALUES (?,?,?)",
                (m, "cidA", 0.01))
            store._conn.execute(
                "INSERT INTO reward_samples (minute_ts, cid, est_usd) VALUES (?,?,?)",
                (m, "cidB", 0.02))
        store._conn.commit()

    rate = store.reward_rate_recent(60)
    # 10 distinct minutes, $0.30 total -> $0.03/min -> $1.80/hr.
    assert rate["minutes"] == 10
    assert abs(rate["usd"] - 0.30) < 1e-9
    assert abs(rate["usd_per_hr"] - 1.80) < 1e-9

    by_mkt = store.reward_rate_by_market(now_min - 9)
    store.close()
    assert abs(by_mkt["cidA"]["usd"] - 0.10) < 1e-9
    assert abs(by_mkt["cidA"]["usd_per_hr"] - 0.60) < 1e-9
    assert abs(by_mkt["cidB"]["usd_per_hr"] - 1.20) < 1e-9


def test_reward_rate_recent_empty_window(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    rate = store.reward_rate_recent(60)
    store.close()
    assert rate == {"usd": 0.0, "minutes": 0, "usd_per_hr": 0.0}


def test_hedge_pnl_uses_maker_basis(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()
    # We hold a YES maker leg bought at 0.55; basis for cid1 = 0.55.
    store.record_fill({
        "ts": ts, "cid": "cid1", "market": "M", "side": "YES",
        "token": "y", "price": 0.55, "size": 10,
    })
    # Forced hedge buys the NO complement at 0.50 -> pair cost 1.05 -> loss 0.05/sh.
    store.record_hedge("cid1", 0.50, 10)
    out = store.hedge_pnl_totals()
    store.close()
    # 10 * (1 - 0.50 - 0.55) = -0.5
    assert abs(out["pnl_total"] - (-0.5)) < 1e-9
    assert abs(out["pnl_24h"] - (-0.5)) < 1e-9
    assert abs(out["spend_total"] - 5.0) < 1e-9
    assert out["shares_total"] == 10


def test_trading_pnl_ledger_reconciles_cashflows(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()
    # Assemble one 10-pair batch: buy YES @0.46 and NO @0.56 (cost 1.02/pair),
    # then merge 10 pairs ($1 each). Net = 10*1 - (4.6 + 5.6) = -0.20.
    store.record_fill({"ts": ts, "cid": "c", "market": "M", "side": "YES",
                       "token": "y", "price": 0.46, "size": 10})
    store.record_fill({"ts": ts, "cid": "c", "market": "M", "side": "NO",
                       "token": "n", "price": 0.56, "size": 10, "taker": True})
    store.record_merge("c", 10)
    # A reduce-only exit sells 2 shares @0.48 (cash in), and a fee is charged.
    store.record_fill({"ts": ts, "cid": "c", "market": "M", "side": "YES",
                       "token": "y", "price": 0.48, "size": 2, "exit": True,
                       "fee": 0.01})
    # Inventory mark for the mark-to-market line.
    store.record_equity(100.0, 3.5)
    out = store.trading_pnl_ledger()
    store.close()
    # merges 10 + sells 0.96 - buys 10.20 - fees 0.01 = +0.75
    assert abs(out["realized_total"] - 0.75) < 1e-9
    assert abs(out["realized_24h"] - 0.75) < 1e-9
    assert abs(out["inventory_usd"] - 3.5) < 1e-9
    assert abs(out["mtm_total"] - (0.75 + 3.5)) < 1e-9


def test_trading_pnl_ledger_empty(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    out = store.trading_pnl_ledger()
    store.close()
    assert out == {"realized_total": 0.0, "realized_24h": 0.0,
                   "inventory_usd": 0.0, "mtm_total": 0.0}


def test_inception_date_prunes_and_blocks(tmp_path):
    db = tmp_path / "test.db"
    # Seed pre-inception rows without the floor.
    seed = MetricsStore(str(db))
    from datetime import datetime, timezone
    old_ts = datetime(2026, 6, 10, tzinfo=timezone.utc).timestamp()
    new_ts = datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp()
    seed.record_fill({"ts": old_ts, "cid": "c", "market": "M", "side": "YES",
                      "token": "y", "price": 0.5, "size": 10})
    seed.record_fill({"ts": new_ts, "cid": "c", "market": "M", "side": "YES",
                      "token": "y", "price": 0.5, "size": 10})
    seed.record_realized_reward("2026-06-10", 5.0)
    seed.record_realized_reward("2026-06-15", 4.0)
    with seed._lock:
        seed._conn.execute(
            "INSERT INTO reward_samples (minute_ts, cid, est_usd) VALUES (?,?,?)",
            (int(old_ts) // 60, "c", 0.01))
        seed._conn.execute(
            "INSERT INTO reward_samples (minute_ts, cid, est_usd) VALUES (?,?,?)",
            (int(new_ts) // 60, "c", 0.02))
        seed._conn.commit()
    seed.close()

    # Reopen with an inception floor: pre-Jun-14 rows are pruned on startup.
    store = MetricsStore(str(db), inception_date="2026-06-14")
    assert store._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM reward_samples").fetchone()[0] == 1
    assert store.reward_totals()["realized_total"] == 4.0

    # A backfill must never (re)write a pre-inception date.
    class FakeClient:
        def get_total_earnings_for_user_for_day(self, date):
            return [{"earnings": 99.0, "asset_rate": 1.0}]

    out = store.backfill_realized_rewards(FakeClient(), days=10)
    store.close()
    assert all(d >= "2026-06-14" for d in out)
    assert "2026-06-10" not in out
