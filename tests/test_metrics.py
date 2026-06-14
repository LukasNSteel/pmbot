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
