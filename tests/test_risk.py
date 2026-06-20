"""Tests for risk.py guards and markout tracker."""

import time

from pmbot.gamma import Market
from pmbot.risk import MarkoutTracker, MarketGuards, RiskManager


CFG = {
    "capital_usd": 500,
    "risk": {
        "scale_with_equity": False,
        "daily_loss_limit_usd": 25,
        "hard_kill_loss_usd": 50,
        "max_total_inventory_usd": 250,
        "theme_max_inventory_usd": 25,
        "theme_groups": {"iran": ["iran"]},
    },
    "guards": {
        "vol_window_secs": 60,
        "vol_max_move_cents": 3.0,
        "max_same_side_fills": 3,
        "same_side_window_minutes": 15,
        "market_cooldown_minutes": 45,
        "velocity_window_secs": 10,
        "velocity_max_trades": 8,
        "directional_consecutive": 5,
        "side_cooldown_minutes": 10,
        "flow_window_secs": 300,
        "flow_min_volume_shares": 200,
        "flow_widen_threshold": 0.6,
        "flow_pull_threshold": 0.85,
        "flow_widen_max_cents": 2.0,
        "markout_horizons_secs": [30, 300],
        "markout_window_minutes": 120,
        "markout_min_samples": 3,
        "markout_trip_cents": -1.5,
    },
}


def _market(question="Will Iran close airspace?", event_id=None) -> Market:
    return Market(
        question=question, condition_id="cid1",
        yes_token="y1", no_token="n1", min_size=10,
        max_spread_cents=3, daily_pool=50, liquidity=1000,
        volume_24h=500, tick=0.01, end_date=None, neg_risk=False,
        event_id=event_id,
    )


def test_risk_manager_smoothed_equity_delays_trip():
    from pmbot.risk import RiskAction
    rm = RiskManager(CFG, 500.0)
    for _ in range(5):
        assert rm.check(480.0, 0) == RiskAction.OK
    assert rm.check(470.0, 0) == RiskAction.OK


def test_risk_manager_pauses_when_equity_unknown():
    from pmbot.risk import RiskAction
    rm = RiskManager(CFG, 500.0)
    assert rm.check(float("nan"), 0) == RiskAction.PAUSE_QUOTES


def test_risk_manager_pauses_when_total_inventory_over_cap():
    from pmbot.risk import RiskAction
    rm = RiskManager(CFG, 500.0)
    assert rm.check(500.0, 251.0) == RiskAction.PAUSE_QUOTES


def test_market_themes_includes_event_id():
    rm = RiskManager(CFG, 500.0)
    m = _market(event_id="evt-123")
    themes = rm.market_themes(m)
    assert "event:evt-123" in themes


def test_markout_tracker_market_avg():
    mt = MarkoutTracker(CFG)
    mt._samples["cid1"] = [(time.time(), 300.0, -0.02), (time.time(), 300.0, -0.01)]
    avg = mt.market_avg("cid1")
    assert avg is not None
    assert avg < 0


def test_flow_imbalance_returns_signed_value():
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()
    for _ in range(250):
        g.record_trade(m, m.yes_token, "BUY", 10, now)
    imb = g.flow_imbalance(m, now)
    assert imb > 0


def test_guard_trip_fires_on_trip_callback_once():
    g = MarketGuards(CFG)
    fired = []
    g.on_trip = fired.append
    m = _market()
    now = time.time()
    g.trip_market(m.condition_id, now, "test", m.question)
    g.trip_market(m.condition_id, now, "test", m.question)  # already tripped
    assert fired == [m.condition_id]


def test_paused_cids_reports_only_active_cooldowns():
    g = MarketGuards(CFG)
    now = time.time()
    g.trip_market("cid_active", now, "test", "q")
    assert g.paused_cids(now) == {"cid_active"}
    # After the cooldown elapses it is no longer reported.
    assert g.paused_cids(now + g.cooldown + 1) == set()


def test_tripping_one_bracket_cools_down_sibling_event_markets():
    g = MarketGuards(CFG)
    now = time.time()
    bracket_a = Market(
        question="Toy Story 5 box office 150-160M", condition_id="cidA",
        yes_token="ya", no_token="na", min_size=10, max_spread_cents=3,
        daily_pool=80, liquidity=1000, volume_24h=500, tick=0.01,
        end_date=None, neg_risk=True, event_id="evt-toy",
    )
    bracket_b = Market(
        question="Toy Story 5 box office 160-170M", condition_id="cidB",
        yes_token="yb", no_token="nb", min_size=10, max_spread_cents=3,
        daily_pool=80, liquidity=1000, volume_24h=500, tick=0.01,
        end_date=None, neg_risk=True, event_id="evt-toy",
    )
    other = Market(
        question="Unrelated market", condition_id="cidC",
        yes_token="yc", no_token="nc", min_size=10, max_spread_cents=3,
        daily_pool=200, liquidity=1000, volume_24h=500, tick=0.01,
        end_date=None, neg_risk=False, event_id="evt-other",
    )
    g.register_markets([bracket_a, bracket_b, other])

    g.trip_market(bracket_a.condition_id, now, "test", bracket_a.question)
    # The sibling bracket is blocked even though it never tripped itself.
    assert not g.allow(bracket_b.condition_id, now)
    assert "evt-toy" in g.paused_event_ids(now)
    assert g.paused_cids(now) == {"cidA", "cidB"}
    # An unrelated event is untouched.
    assert g.allow(other.condition_id, now)
    # Everything clears once the cooldown elapses.
    later = now + g.cooldown + 1
    assert g.allow(bracket_b.condition_id, later)
    assert g.paused_event_ids(later) == set()


def test_directional_flow_fires_side_block_callback():
    g = MarketGuards(CFG)
    blocked = []
    g.on_side_block = blocked.append
    m = _market()
    now = time.time()
    for _ in range(g.dir_consec):
        g.record_trade(m, m.yes_token, "SELL", 1, now)
    assert blocked == [m.yes_token]


def test_flow_pull_fires_side_block_callback():
    from collections import deque

    g = MarketGuards(CFG)
    blocked = []
    g.on_side_block = blocked.append
    m = _market()
    now = time.time()
    # 250 shares of one-sided YES buying — above pull threshold.
    g._flow[m.condition_id] = deque([(now, 10.0)] * 25)
    g.check_flow(m, now)
    assert blocked == [m.no_token]
    g.check_flow(m, now)  # already blocked — no second fire
    assert blocked == [m.no_token]
