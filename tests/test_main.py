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
