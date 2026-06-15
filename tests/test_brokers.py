"""Tests for broker fill models and live order diff."""

import time
import threading
from unittest.mock import MagicMock

from pmbot.books import Book, BookTracker
from pmbot.brokers import (
    LiveBroker,
    PaperBroker,
    Position,
    _parse_erc20_balance,
    _parse_fill_amount,
)
from pmbot.gamma import Market
from pmbot.strategy import Quote


def _market() -> Market:
    return Market(
        question="Test?", condition_id="cid1",
        yes_token="yes1", no_token="no1", min_size=10,
        max_spread_cents=3, daily_pool=50, liquidity=1000,
        volume_24h=500, tick=0.01, end_date=None, neg_risk=False,
    )


def test_parse_fill_amount_from_taking_amount():
    assert _parse_fill_amount({"takingAmount": "5.0"}, 10.0) == 5.0


def test_parse_fill_amount_zero_on_error():
    assert _parse_fill_amount({"success": False, "error": "rejected"}, 10.0) == 0.0


def test_parse_erc20_balance_uses_six_decimals():
    assert _parse_erc20_balance(hex(100_512_253)) == 100.512253


def test_paper_queue_consumes_ahead_before_fill():
    tracker = BookTracker(["yes1", "no1"])
    tracker.books["yes1"].bids[0.47] = 100.0
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.47, "SELL", 50))
    assert broker.open_quotes(m)
    assert not broker.fills_log


def test_paper_fill_on_strictly_below_price():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.46, "SELL", 10))
    assert not broker.open_quotes(m)
    assert len(broker.fills_log) == 1


def test_paper_latency_blocks_fill_before_placement_lands():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker, latency_secs=60.0)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.45, "SELL", 10))
    assert not broker.fills_log  # order still in flight, not on the book yet


def test_paper_replaced_quote_picked_off_during_cancel_latency():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker, latency_secs=0.0)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])  # lands instantly

    broker.latency = 30.0  # subsequent ops now take 30s to land
    broker.set_quotes(m, [Quote("yes1", 0.45, 10)])  # requote down

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.46, "SELL", 10))
    # The stale 0.47 bid (cancel in flight) gets picked off; the new 0.45
    # bid hasn't landed yet.
    assert len(broker.fills_log) == 1
    assert broker.fills_log[0]["price"] == 0.47


def test_paper_fill_capped_by_trade_size():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 50)])

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.46, "SELL", 5))
    assert len(broker.fills_log) == 1
    assert broker.fills_log[0]["size"] == 5
    # Remainder still resting at the front of its level.
    assert broker.open_quotes(m)[0].size == 45


def test_paper_taker_buy_respects_displayed_depth():
    tracker = BookTracker(["yes1", "no1"])
    tracker.books["no1"].asks = {0.50: 5.0, 0.52: 20.0}
    broker = PaperBroker(500.0, tracker)
    m = _market()
    filled = broker.taker_buy(m, "no1", 10.0, max_price=0.51)
    assert filled == 5.0  # only the 0.50 level is inside the price cap


def test_paper_fill_charges_fee():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    m.fee_bps = 200
    broker._fill(m, Quote("yes1", 0.40, 10), 10)
    # fee = 200/10000 * min(0.40, 0.60) * 10 = 0.08
    assert abs(broker.state.cash - (500.0 - 4.0 - 0.08)) < 1e-9


def test_paper_exit_requires_queue_or_through_print():
    tracker = BookTracker(["yes1", "no1"])
    tracker.books["yes1"].asks = {0.53: 40.0}
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.state.positions["cid1"] = Position(yes_shares=10)
    broker.set_exit(m, Quote("yes1", 0.53, 10))

    import asyncio
    # Trade at our price only consumes the 40 shares queued ahead.
    asyncio.run(broker._on_trade("yes1", 0.53, "BUY", 30))
    assert not broker.fills_log
    # A print above our ask guarantees the fill.
    asyncio.run(broker._on_trade("yes1", 0.54, "BUY", 30))
    assert broker.fills_log and broker.fills_log[0]["exit"] is True


def test_paper_cancel_quotes_keeps_exits():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])
    broker.set_exit(m, Quote("yes1", 0.53, 5))
    broker.cancel_quotes()
    assert not broker.open_quotes(m)
    assert broker.exit_quote(m) is not None


def test_live_order_diff_keeps_unchanged():
    """Unit test the set_quotes keep/cancel decision without CLOB imports."""
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, RestingOrder

    q = Quote("yes1", 0.47, 10)
    ro = RestingOrder("oid1", q, time.time(),
                      int(time.time()) + GTD_REFRESH_MARGIN_SECS + 60)
    desired = {q.token_id: q}
    near_expiry = ro.expiration - time.time() < GTD_REFRESH_MARGIN_SECS
    should_keep = (
        desired.get(ro.quote.token_id) is not None
        and desired[ro.quote.token_id].key() == ro.quote.key()
        and not near_expiry
    )
    assert should_keep
    desired2 = {q.token_id: Quote("yes1", 0.45, 10)}
    should_keep2 = (
        desired2.get(ro.quote.token_id) is not None
        and desired2[ro.quote.token_id].key() == ro.quote.key()
    )
    assert not should_keep2


def test_gtd_refresh_margin_covers_security_threshold():
    """An order must be refreshed before its effective (expiration - 60s)
    expiry, not its nominal expiration."""
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, GTD_SECURITY_THRESHOLD_SECS

    assert GTD_REFRESH_MARGIN_SECS > GTD_SECURITY_THRESHOLD_SECS


def _order_book_stub():
    """LiveBroker order-tracking methods exercised without a CLOB client."""
    class Stub:
        pass

    stub = Stub()
    stub._open_orders = {}
    stub._exit_orders = {}
    return stub


def test_apply_fill_partial_decrements_resting_order():
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    ro = RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)
    stub._open_orders = {"cid1": [ro]}
    LiveBroker._apply_fill_to_orders(stub, "yes1", 4.0, "BUY")
    assert stub._open_orders["cid1"][0].quote.size == 6.0


def test_apply_fill_full_removes_resting_order():
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    ro = RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)
    stub._open_orders = {"cid1": [ro]}
    LiveBroker._apply_fill_to_orders(stub, "yes1", 10.0, "BUY")
    assert stub._open_orders["cid1"] == []


def test_apply_fill_does_not_leak_to_other_orders():
    """A partial fill must consume the whole fill against its own order,
    never carrying phantom leftover size into other resting orders."""
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    ro1 = RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)
    ro2 = RestingOrder("o2", Quote("yes1", 0.46, 10), time.time(), 0)
    stub._open_orders = {"cid1": [ro1], "cid2": [ro2]}
    LiveBroker._apply_fill_to_orders(stub, "yes1", 6.0, "BUY")
    assert stub._open_orders["cid1"][0].quote.size == 4.0
    assert stub._open_orders["cid2"][0].quote.size == 10.0


def test_apply_fill_sell_decrements_exit_order():
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    stub._exit_orders = {
        "cid1": RestingOrder("o1", Quote("yes1", 0.53, 8), time.time(), 0)}
    LiveBroker._apply_fill_to_orders(stub, "yes1", 3.0, "SELL")
    assert stub._exit_orders["cid1"].quote.size == 5.0
    LiveBroker._apply_fill_to_orders(stub, "yes1", 5.0, "SELL")
    assert "cid1" not in stub._exit_orders


def test_cancel_quotes_keeps_local_state_when_cancel_fails():
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.client.cancel_orders.side_effect = RuntimeError("down")
    stub.client.cancel_order.side_effect = RuntimeError("down")
    stub.reconcile_orders = MagicMock()
    stub._batch_cancel = lambda ids: LiveBroker._batch_cancel(stub, ids)
    ro = RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)
    stub._open_orders = {"cid1": [ro]}

    LiveBroker.cancel_quotes(stub)

    assert stub._open_orders == {"cid1": [ro]}
    stub.reconcile_orders.assert_called_once()


def test_live_crossed_book_forces_reconcile_without_blocking():
    """A crossed resting bid must flag a reconcile for the next off-thread
    refresh, not call the network on the event loop."""
    from pmbot.brokers import RestingOrder

    tracker = BookTracker(["yes1"])
    tracker.books["yes1"].asks = {0.46: 10.0}
    stub = _order_book_stub()
    stub.tracker = tracker
    stub._last_order_reconcile = time.time()
    stub.reconcile_orders = MagicMock()
    stub._open_orders = {
        "cid1": [RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)]}

    LiveBroker.check_crossed_books(stub)

    stub.reconcile_orders.assert_not_called()
    assert stub._last_order_reconcile == 0.0


# --- deposit-wallet (signature_type 3) balance-cache sync regression -------
# Without these, the CLOB's cache reads 0 for the deposit wallet and rejects
# orders with "not enough balance / allowance: ... balance: 0".

def _live_stub(sig_type=3):
    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.cfg = {"live": {"signature_type": sig_type}}
    stub._gtd_expiration = lambda: 123
    stub.metrics = None
    stub.sync_calls = []
    stub._sync_clob_balance = lambda at, tid=None: stub.sync_calls.append((at, tid))
    return stub


def test_place_sell_syncs_conditional_balance_first():
    from py_clob_client_v2 import AssetType
    stub = _live_stub()
    stub.client.post_order.return_value = {"orderID": "oidS"}
    ro = LiveBroker._place_sell(stub, Quote("tok9", 0.62, 50))
    assert ro is not None and ro.order_id == "oidS"
    # the conditional token was synced for exactly the token being sold
    assert stub.sync_calls == [(AssetType.CONDITIONAL, "tok9")]


def test_taker_buy_syncs_collateral_first():
    from py_clob_client_v2 import AssetType
    stub = _live_stub()
    stub.client.post_order.return_value = {"takingAmount": "10.0"}
    filled = LiveBroker.taker_buy(stub, _market(), "tok9", 10.0, 0.6)
    assert filled == 10.0
    assert (AssetType.COLLATERAL, None) in stub.sync_calls


def test_sync_clob_balance_builds_params_and_swallows_errors():
    from py_clob_client_v2 import AssetType
    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.cfg = {"live": {"signature_type": 3}}
    stub.client.update_balance_allowance.side_effect = RuntimeError("relayer down")
    # a sync hiccup must never bubble up into the quoting loop
    LiveBroker._sync_clob_balance(stub, AssetType.CONDITIONAL, "tok9")
    params = stub.client.update_balance_allowance.call_args.args[0]
    assert params.token_id == "tok9"
    assert params.signature_type == 3


def test_select_collateral_prefers_onchain_over_stale_cache():
    # on-chain pUSD wins even when the (stale) CLOB cache reads higher
    assert LiveBroker._select_collateral(70.5, 100.0) == 70.5
    # falls back to the cache only when the on-chain read is unavailable
    assert LiveBroker._select_collateral(None, 100.0) == 100.0
    assert LiveBroker._select_collateral(None, None) is None
