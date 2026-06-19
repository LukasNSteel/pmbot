"""Tests for Bot event-driven quote pulls (main.py)."""

import asyncio
import time

from pmbot.books import BookTracker
from pmbot.brokers import PaperBroker
from pmbot.gamma import Market
from pmbot.main import Bot
from pmbot.strategy import Quote


BASE_CFG = {
    "mode": "paper",
    "capital_usd": 500,
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


def _market() -> Market:
    return Market(
        question="Will it rain tomorrow?", condition_id="cid1",
        yes_token="y1", no_token="n1", min_size=10,
        max_spread_cents=3, daily_pool=50, liquidity=1000,
        volume_24h=500, tick=0.01, end_date=None, neg_risk=False,
    )


def _bot(tmp_path) -> Bot:
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    return Bot(cfg)


def _setup(bot: Bot, tmp_path, market: Market) -> PaperBroker:
    tracker = BookTracker([market.yes_token, market.no_token])
    broker = PaperBroker(500.0, tracker, data_dir=str(tmp_path))
    bot.tracker = tracker
    bot.broker = broker
    bot.markets = [market]
    bot._token_market = {market.yes_token: market, market.no_token: market}
    return broker


def test_guard_trip_pulls_quotes_immediately(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        m = _market()
        broker = _setup(bot, tmp_path, m)
        broker.set_quotes(m, [Quote(m.yes_token, 0.45, 20.0),
                              Quote(m.no_token, 0.52, 20.0)])
        bot.guards.trip_market(m.condition_id, time.time(), "test", m.question)
        assert bot._pull_tasks  # pull scheduled without waiting for the loop
        await asyncio.gather(*list(bot._pull_tasks))
        assert broker.open_quotes(m) == []
        bot.metrics.close()
    asyncio.run(scenario())


def test_side_block_pulls_only_blocked_side(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        m = _market()
        broker = _setup(bot, tmp_path, m)
        broker.set_quotes(m, [Quote(m.yes_token, 0.45, 20.0),
                              Quote(m.no_token, 0.52, 20.0)])
        now = time.time()
        for _ in range(bot.guards.dir_consec):
            bot.guards.record_trade(m, m.yes_token, "SELL", 1.0, now)
        assert bot._pull_tasks
        await asyncio.gather(*list(bot._pull_tasks))
        remaining = broker.open_quotes(m)
        assert [q.token_id for q in remaining] == [m.no_token]
        bot.metrics.close()
    asyncio.run(scenario())


def test_schedule_pull_without_running_loop_is_noop(tmp_path):
    bot = _bot(tmp_path)
    bot._schedule_market_pull("cid1")  # must not raise outside a loop
    assert not bot._pull_tasks
    bot.metrics.close()


def _scored(cid: str, score: float) -> Market:
    m = Market(
        question=f"market {cid}", condition_id=cid,
        yes_token=f"{cid}y", no_token=f"{cid}n", min_size=10,
        max_spread_cents=3, daily_pool=100, liquidity=5000,
        volume_24h=0, tick=0.01, end_date=None, neg_risk=False,
    )
    m.score = score
    return m


def test_sticky_keeps_held_market_over_marginally_better(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25}
    bot.markets = [_scored("held", 1.0)]
    # a newcomer scoring 1.1 does NOT clear 0.9 * 1.25 = 1.125 → held is kept.
    chosen = bot._select_markets([_scored("new", 1.1), _scored("held", 0.9)])
    assert [m.condition_id for m in chosen] == ["held"]
    bot.metrics.close()


def _seed_uptime(bot, cid: str, in_band: bool, minutes: int = 15) -> None:
    now_min = int(time.time() // 60)
    with bot.metrics._lock:
        for i in range(minutes):
            bot.metrics._conn.execute(
                "INSERT INTO uptime (minute_ts, cid, in_band) VALUES (?,?,?)",
                (now_min - i, cid, int(in_band)))
        bot.metrics._conn.commit()


def test_sticky_displaces_underperforming_held_on_large_margin(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25, "underperform_uptime_pct": 60}
    bot.markets = [_scored("held", 1.0)]
    _seed_uptime(bot, "held", in_band=False)  # 0% uptime → underperforming
    # 2.0 >= 1.0 * 1.25 AND held underperforming → the better market wins.
    chosen = bot._select_markets([_scored("new", 2.0), _scored("held", 1.0)])
    assert [m.condition_id for m in chosen] == ["new"]
    bot.metrics.close()


def test_sticky_protects_performing_held_even_from_much_better(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25, "underperform_uptime_pct": 60}
    bot.markets = [_scored("held", 1.0)]
    _seed_uptime(bot, "held", in_band=True)  # 100% uptime → farming well
    # Even a 10x-better candidate must NOT evict a market that is performing.
    chosen = bot._select_markets([_scored("new", 10.0), _scored("held", 1.0)])
    assert [m.condition_id for m in chosen] == ["held"]
    bot.metrics.close()


def test_sticky_protects_freshly_entered_held_with_thin_history(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25, "underperform_uptime_pct": 60}
    bot.markets = [_scored("held", 1.0)]
    _seed_uptime(bot, "held", in_band=False, minutes=3)  # below min_samples
    # Too little history to judge → treated as performing → protected.
    chosen = bot._select_markets([_scored("new", 10.0), _scored("held", 1.0)])
    assert [m.condition_id for m in chosen] == ["held"]
    bot.metrics.close()


def test_sticky_drops_ineligible_held_and_backfills(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25}
    bot.markets = [_scored("held", 1.0)]
    # held no longer appears in the ranked (ineligible) → slot backfills.
    chosen = bot._select_markets([_scored("other", 0.5)])
    assert [m.condition_id for m in chosen] == ["other"]
    bot.metrics.close()


def test_rescan_is_sticky_and_swaps_incrementally(tmp_path, monkeypatch):
    """End-to-end: a reshuffled re-rank must not churn the set, and a genuine
    swap must reuse the tracker (resubscribe) instead of stop()/start()."""
    from pmbot import gamma as gamma_mod
    from pmbot.books import BookTracker

    counts = {"resub": 0, "stop": 0, "start": 0}

    async def fake_start(self):
        counts["start"] += 1

    async def fake_stop(self):
        counts["stop"] += 1

    async def fake_resub(self, token_ids, carry=None):
        counts["resub"] += 1
        self.books = {t: self.books.get(t) or __import__(
            "pmbot.books", fromlist=["Book"]).Book(t) for t in token_ids}

    monkeypatch.setattr(BookTracker, "start", fake_start)
    monkeypatch.setattr(BookTracker, "stop", fake_stop)
    monkeypatch.setattr(BookTracker, "resubscribe", fake_resub)

    ranked_holder = {"v": []}
    monkeypatch.setattr(gamma_mod, "scan",
                        lambda cfg, exclude=None, full=False: list(ranked_holder["v"]))

    async def scenario():
        bot = _bot(tmp_path)
        bot.cfg = dict(bot.cfg)
        bot.cfg["paper"] = {}
        bot.cfg["risk"] = {}
        bot.cfg["quoting"] = {"max_capital_per_market": 50}
        bot.cfg["scanner"] = {
            "top_n_markets": 2, "sticky_swap": True, "swap_score_margin": 0.25,
            "underperform_uptime_pct": 60, "underperform_lookback_minutes": 30,
            "refresh_minutes": 30,
        }

        a, b, c = _scored("A", 3.0), _scored("B", 2.0), _scored("C", 1.0)
        ranked_holder["v"] = [a, b, c]
        await bot._rescan(initial=True)
        assert {m.condition_id for m in bot.markets} == {"A", "B"}
        assert counts["start"] == 1 and counts["resub"] == 0

        # Re-rank reshuffles scores but the same cids stay best → NO churn.
        ranked_holder["v"] = [_scored("B", 3.0), _scored("A", 2.0), _scored("C", 1.0)]
        await bot._rescan()
        assert {m.condition_id for m in bot.markets} == {"A", "B"}
        assert counts["resub"] == 0 and counts["stop"] == 0  # nothing torn down

        # A held market (A) drops out of eligibility → real swap to C.
        ranked_holder["v"] = [_scored("B", 3.0), _scored("C", 1.0)]
        await bot._rescan()
        assert {m.condition_id for m in bot.markets} == {"B", "C"}
        assert counts["resub"] == 1   # incremental resubscribe used…
        assert counts["stop"] == 0    # …and the tracker was never torn down
        bot.metrics.close()

    asyncio.run(scenario())


def test_sticky_disabled_returns_plain_top_n(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 2, "sticky_swap": False}
    bot.markets = [_scored("held", 1.0)]
    chosen = bot._select_markets(
        [_scored("a", 3), _scored("b", 2), _scored("c", 1)])
    assert [m.condition_id for m in chosen] == ["a", "b"]
    bot.metrics.close()
