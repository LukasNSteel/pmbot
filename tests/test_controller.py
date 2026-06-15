"""Tests for the adaptive controller: capital tiers + toxicity interpolation."""

import copy
import time

from pmbot.controller import AdaptiveController
from pmbot.risk import MarketGuards, MarkoutTracker


BASE_CFG = {
    "capital_usd": 100,
    "scanner": {"top_n_markets": 2},
    "quoting": {"offset_frac_of_max_spread": 0.35},
    "risk": {
        "max_inventory_usd_per_market": 60.0,
        "daily_loss_limit_usd": 25.0,
        "hard_kill_loss_usd": 50.0,
        "flatten_after_secs": 90.0,
        "flatten_max_spread_cents": 4.0,
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
        "markout_min_samples": 5,
        "markout_trip_cents": -1.5,
    },
    "controller": {
        "enabled": True,
        "interval_minutes": 10,
        "smoothing": 1.0,  # jump straight to target for deterministic asserts
        "markout_calm_cents": 0.0,
        "markout_toxic_cents": -3.0,
        "min_markout_samples": 4,
        "calm": {
            "offset_frac_of_max_spread": 0.30,
            "flatten_after_secs": 120,
            "flatten_max_spread_cents": 4.0,
            "markout_trip_cents": -1.5,
            "markout_min_samples": 5,
        },
        "toxic": {
            "offset_frac_of_max_spread": 0.60,
            "flatten_after_secs": 360,
            "flatten_max_spread_cents": 2.0,
            "markout_trip_cents": -0.8,
            "markout_min_samples": 2,
        },
        "capital_tiers": [
            {"min_equity_usd": 0, "top_n_markets": 1,
             "max_inventory_usd_per_market": 25, "daily_loss_limit_usd": 8,
             "hard_kill_loss_usd": 20},
            {"min_equity_usd": 250, "top_n_markets": 2,
             "max_inventory_usd_per_market": 50, "daily_loss_limit_usd": 20,
             "hard_kill_loss_usd": 50},
            {"min_equity_usd": 750, "top_n_markets": 3,
             "max_inventory_usd_per_market": 90, "daily_loss_limit_usd": 50,
             "hard_kill_loss_usd": 120},
        ],
    },
}


def _make(cfg):
    guards = MarketGuards(cfg)
    markouts = MarkoutTracker(cfg)
    ctrl = AdaptiveController(cfg, guards, markouts)
    return cfg, guards, markouts, ctrl


def _seed_markouts(markouts, markout_cents, n):
    """Inject n long-horizon markout samples at the given cents value."""
    h = max(markouts.horizons)
    now = time.time()
    markouts._samples["cid-test"] = [
        (now, h, markout_cents / 100.0) for _ in range(n)
    ]


def test_low_capital_picks_conservative_tier():
    cfg, _, _, ctrl = _make(copy.deepcopy(BASE_CFG))
    ctrl.apply(equity=90.0)
    assert cfg["scanner"]["top_n_markets"] == 1
    assert cfg["risk"]["max_inventory_usd_per_market"] == 25.0
    assert cfg["risk"]["daily_loss_limit_usd"] == 8.0
    assert cfg["risk"]["hard_kill_loss_usd"] == 20.0
    assert ctrl.active_tier_equity == 0


def test_higher_capital_scales_up():
    cfg, _, _, ctrl = _make(copy.deepcopy(BASE_CFG))
    ctrl.apply(equity=800.0)
    assert cfg["scanner"]["top_n_markets"] == 3
    assert cfg["risk"]["max_inventory_usd_per_market"] == 90.0
    assert ctrl.active_tier_equity == 750


def test_calm_flow_tightens_quotes():
    cfg, _, markouts, ctrl = _make(copy.deepcopy(BASE_CFG))
    _seed_markouts(markouts, markout_cents=+1.0, n=10)  # benign
    ctrl.apply(equity=90.0)
    assert ctrl.toxicity == 0.0
    assert cfg["quoting"]["offset_frac_of_max_spread"] == 0.30
    assert cfg["risk"]["flatten_after_secs"] == 120


def test_toxic_flow_widens_quotes_and_slows_hedge():
    cfg, _, markouts, ctrl = _make(copy.deepcopy(BASE_CFG))
    _seed_markouts(markouts, markout_cents=-5.0, n=10)  # very toxic
    ctrl.apply(equity=90.0)
    assert ctrl.toxicity == 1.0
    assert cfg["quoting"]["offset_frac_of_max_spread"] == 0.60
    assert cfg["risk"]["flatten_after_secs"] == 360
    assert cfg["risk"]["flatten_max_spread_cents"] == 2.0


def test_partial_toxicity_interpolates():
    cfg, _, markouts, ctrl = _make(copy.deepcopy(BASE_CFG))
    _seed_markouts(markouts, markout_cents=-1.5, n=10)  # halfway to toxic
    ctrl.apply(equity=90.0)
    assert ctrl.toxicity == 0.5
    # offset halfway between 0.30 and 0.60.
    assert abs(cfg["quoting"]["offset_frac_of_max_spread"] - 0.45) < 1e-9


def test_insufficient_samples_stay_calm():
    cfg, _, markouts, ctrl = _make(copy.deepcopy(BASE_CFG))
    _seed_markouts(markouts, markout_cents=-9.0, n=2)  # toxic but too few
    ctrl.apply(equity=90.0)
    assert ctrl.toxicity == 0.0
    assert cfg["quoting"]["offset_frac_of_max_spread"] == 0.30


def test_reload_propagates_trip_cents_to_markout_tracker():
    cfg, _, markouts, ctrl = _make(copy.deepcopy(BASE_CFG))
    assert markouts.trip_cents == -1.5
    _seed_markouts(markouts, markout_cents=-5.0, n=10)
    ctrl.apply(equity=90.0)
    # toxic anchor sets guards.markout_trip_cents to -0.8 and reload pushes it
    # into the live MarkoutTracker.
    assert cfg["guards"]["markout_trip_cents"] == -0.8
    assert markouts.trip_cents == -0.8
    # toxic anchor also tightens min_samples 5 -> 2 so small books trip faster.
    assert cfg["guards"]["markout_min_samples"] == 2
    assert markouts.min_samples == 2


def test_smoothing_moves_partway():
    cfg = copy.deepcopy(BASE_CFG)
    cfg["controller"]["smoothing"] = 0.5
    cfg, _, markouts, ctrl = _make(cfg)
    _seed_markouts(markouts, markout_cents=-5.0, n=10)  # target offset 0.60
    ctrl.apply(equity=90.0)
    # from 0.35 toward 0.60 at 0.5 smoothing -> 0.475.
    assert abs(cfg["quoting"]["offset_frac_of_max_spread"] - 0.475) < 1e-9


def test_disabled_controller_is_noop():
    cfg = copy.deepcopy(BASE_CFG)
    cfg["controller"]["enabled"] = False
    cfg, _, _, ctrl = _make(cfg)
    applied = ctrl.maybe_apply(now=time.time(), equity=90.0)
    assert applied is False
    assert cfg["scanner"]["top_n_markets"] == 2  # untouched


def test_interval_gating():
    cfg, _, _, ctrl = _make(copy.deepcopy(BASE_CFG))
    now = time.time()
    assert ctrl.maybe_apply(now, 90.0) is True       # first run always fires
    assert ctrl.maybe_apply(now + 60, 90.0) is False  # within interval
    assert ctrl.maybe_apply(now + 601, 90.0) is True  # interval elapsed
