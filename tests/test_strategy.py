"""Tests for strategy.py quoting and reward estimation."""

from datetime import datetime

from pmbot.books import Book
from pmbot.gamma import Market
from pmbot.strategy import (
    Quote,
    adaptive_offset,
    book_is_quotable,
    compute_quotes,
    microprice,
    reconcile_quotes,
)


def _market(**kw) -> Market:
    defaults = dict(
        question="Test market?",
        condition_id="0xabc",
        yes_token="yes_tok",
        no_token="no_tok",
        min_size=10.0,
        max_spread_cents=3.0,
        daily_pool=50.0,
        liquidity=1000.0,
        volume_24h=500.0,
        tick=0.01,
        end_date=None,
        neg_risk=False,
    )
    defaults.update(kw)
    return Market(**defaults)


def _book(bid=0.48, ask=0.52, bid_sz=100, ask_sz=100) -> Book:
    b = Book("yes_tok")
    b.bids = {bid: bid_sz}
    b.asks = {ask: ask_sz}
    return b


CFG = {
    "scanner": {"mid_range": [0.15, 0.85]},
    "quoting": {
        "offset_frac_of_max_spread": 0.35,
        "size_mult_of_min": 1.0,
        "max_capital_per_market": 90,
        "skew_strength": 0.6,
        "max_book_spread_mult_of_band": 3.0,
        "flow_drift_max_cents": 1.0,
        "adaptive_markout_gain": 1.0,
        "adaptive_tighten_max_cents": 0.5,
        "adaptive_widen_max_cents": 2.0,
    },
}


def test_book_is_quotable_rejects_one_sided():
    b = Book("t")
    b.last_trade_price = 0.5
    assert not book_is_quotable(b, 0.03, 3.0)


def test_book_is_quotable_rejects_wide_spread():
    b = _book(bid=0.40, ask=0.60)
    assert not book_is_quotable(b, 0.03, 3.0)


def test_microprice_weights_by_size():
    b = _book(bid=0.48, ask=0.52, bid_sz=100, ask_sz=300)
    fair = microprice(b)
    assert fair is not None
    assert 0.48 < fair < 0.50


def test_compute_quotes_returns_two_sides():
    m = _market()
    quotes = compute_quotes(m, _book(), 0.0, CFG, 60.0)
    assert len(quotes) == 2
    tokens = {q.token_id for q in quotes}
    assert tokens == {"yes_tok", "no_tok"}


def test_compute_quotes_skew_drops_side_at_cap():
    m = _market()
    quotes = compute_quotes(m, _book(), 60.0, CFG, 60.0)
    assert len(quotes) == 1
    assert quotes[0].token_id == "no_tok"


def test_compute_quotes_size_never_below_min_incentive_size():
    """A low size_factor must not shrink orders below min_size — they would
    score zero rewards."""
    m = _market(min_size=10.0)
    quotes = compute_quotes(m, _book(), 0.0, CFG, 60.0, size_factor=0.5)
    assert quotes
    assert all(q.size >= m.min_size for q in quotes)


def test_compute_quotes_fee_market_widens_out_of_band():
    """A 1000bps fee (~5c/share at mid 0.50) dwarfs the 3c reward band, so a
    fee market must produce no quotes rather than bleed fees on every fill."""
    m = _market(fee_bps=1000)
    assert compute_quotes(m, _book(), 0.0, CFG, 60.0) == []


def test_compute_quotes_small_fee_widens_offset():
    free = compute_quotes(_market(), _book(), 0.0, CFG, 60.0)
    feed = compute_quotes(_market(fee_bps=200), _book(), 0.0, CFG, 60.0)
    assert free and feed
    free_yes = next(q for q in free if q.token_id == "yes_tok")
    fee_yes = next(q for q in feed if q.token_id == "yes_tok")
    assert fee_yes.price < free_yes.price


def test_adaptive_offset_widens_on_negative_markout():
    adj = adaptive_offset(-0.02, CFG)
    assert adj > 0


def test_adaptive_offset_tightens_on_positive_markout():
    adj = adaptive_offset(0.02, CFG)
    assert adj < 0


def test_reconcile_quotes_keeps_close_quotes():
    cur = [Quote("yes_tok", 0.47, 10)]
    des = [Quote("yes_tok", 0.4705, 10)]
    final = reconcile_quotes(cur, des, move_cents=0.4)
    assert final[0] is cur[0]


def test_reconcile_quotes_replaces_distant_quotes():
    cur = [Quote("yes_tok", 0.47, 10)]
    des = [Quote("yes_tok", 0.45, 10)]
    final = reconcile_quotes(cur, des, move_cents=0.4)
    assert final[0] is des[0]
