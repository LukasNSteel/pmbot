"""Quoting engine: two-sided buy quotes (YES bid + NO bid) with inventory skew,
plus an estimator for Polymarket liquidity-reward score share.

Quotes are always buys. A buy on the NO book at price q is equivalent to a sell
on the YES book at 1-q, so quoting BUY YES + BUY NO gives two-sided liquidity
without needing inventory. When both sides fill we hold YES+NO pairs that
merge back to $1, realizing the spread. (The only sells the bot ever places
are reduce-only passive exits on excess inventory — see main._update_exit_sell.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .books import Book
from .gamma import Market

SINGLE_SIDED_DIVISOR = 3.0  # Polymarket's `c` scaling factor


@dataclass
class Quote:
    """Desired resting buy order on one token's book."""

    token_id: str
    price: float
    size: float

    def key(self) -> tuple[str, float, float]:
        return (self.token_id, self.price, self.size)


def _round_tick(price: float, tick: float) -> float:
    steps = round(price / tick)
    return round(steps * tick, 6)


def book_feed_stale(feed_age: float, book_age: float, max_stale: float) -> bool:
    """True when our book view may diverge from reality and quotes must be pulled.

    Feed-level staleness (no websocket traffic for `max_stale`s, heartbeat
    included) means the socket is lagging or dead — the real danger. A book
    that is merely quiet while the feed is alive is safe to keep quoting; idle
    books are prime reward-farming time, not a hazard. A long per-book ceiling
    (4× max_stale, floored at 120s) backstops a silently dropped single-token
    subscription that the feed heartbeat alone would miss (REST resync also
    covers this case)."""
    return feed_age > max_stale or book_age > max(max_stale * 4.0, 120.0)


def book_is_quotable(yes_book: Book, band: float, max_spread_mult: float) -> bool:
    """Require a two-sided book with a sane spread — never quote off last-trade."""
    bb, ba = yes_book.best_bid, yes_book.best_ask
    if bb is None or ba is None:
        return False
    spread = ba - bb
    return spread <= band * max_spread_mult + 1e-9


def microprice(book: Book) -> float | None:
    """Volume-weighted fair value from the top of book."""
    bb, ba = book.best_bid, book.best_ask
    if bb is None or ba is None:
        return None
    bid_sz = book.bids.get(bb, 0.0)
    ask_sz = book.asks.get(ba, 0.0)
    total = bid_sz + ask_sz
    if total <= 0:
        return (bb + ba) / 2
    return (bb * ask_sz + ba * bid_sz) / total


def adaptive_offset(avg_markout: float | None, cfg: dict) -> float:
    """Per-market offset adjustment from average markout (price units).

    Negative markout (picked off) widens; benign markout tightens."""
    if avg_markout is None:
        return 0.0
    q = cfg["quoting"]
    gain = float(q.get("adaptive_markout_gain", 1.0))
    tighten = float(q.get("adaptive_tighten_max_cents", 0.5)) / 100.0
    widen = float(q.get("adaptive_widen_max_cents", 2.0)) / 100.0
    adj = -gain * avg_markout
    return max(-tighten, min(widen, adj))


def compute_quotes(
    market: Market,
    yes_book: Book,
    net_yes_exposure_usd: float,
    cfg: dict,
    max_inventory_usd: float,
    fade_yes: float = 0.0,
    fade_no: float = 0.0,
    scale: float = 1.0,
    flow_imbalance: float = 0.0,
    markout_avg: float | None = None,
    size_factor: float = 1.0,
) -> list[Quote]:
    """Desired quotes for one market given current book + inventory."""
    q = cfg["quoting"]
    mid = yes_book.mid
    if mid is None:
        return []

    band = market.max_spread_cents / 100.0
    max_spread_mult = float(q.get("max_book_spread_mult_of_band", 3.0))
    if not book_is_quotable(yes_book, band, max_spread_mult):
        return []

    lo, hi = cfg["scanner"]["mid_range"]
    if not (lo <= mid <= hi):
        return []

    fair = microprice(yes_book) or mid
    drift_max = float(q.get("flow_drift_max_cents", 1.0)) / 100.0
    fair += flow_imbalance * drift_max

    offset = max(band * q["offset_frac_of_max_spread"], market.tick)
    offset += adaptive_offset(markout_avg, cfg)
    # No fee widening: on Polymarket makers are never charged fees, so resting
    # quotes cost nothing per fill. Widening here would only push us out of the
    # reward band and forfeit rewards. Fees apply solely on taker merges/exits.

    base_size = max(market.min_size * q["size_mult_of_min"] * scale, market.min_size)
    size = float(int(base_size * max(0.5, min(2.0, size_factor))))

    # Clamp size to the per-market capital cap (size in shares ≈ USD committed
    # at $1 pair value). We CLAMP rather than drop: when the size driver
    # (size_mult_of_min / scale) pushes above the cap we still quote as much as
    # the cap allows — only skip the market if even the min incentive size won't
    # fit (the scanner already filters those, but a tier drop can shrink the cap
    # under a held market's min size).
    max_cap = q["max_capital_per_market"] * scale
    if market.min_size > max_cap + 1e-9:
        return []
    size = min(size, float(int(max_cap)))
    # Below min_incentive_size the order scores zero rewards — never go under.
    size = max(size, float(math.ceil(market.min_size)))

    skew_frac = max(-1.0, min(1.0, net_yes_exposure_usd / max(max_inventory_usd, 1e-9)))
    skew = skew_frac * q["skew_strength"] * offset

    yes_bid = _round_tick(fair - offset - skew - fade_yes, market.tick)
    no_mid = 1.0 - mid
    no_fair = 1.0 - fair
    no_bid = _round_tick(no_fair - offset + skew - fade_no, market.tick)

    quotes = []
    if 0 < yes_bid and skew_frac < 1.0 and (mid - yes_bid) <= band + 1e-9:
        quotes.append(Quote(market.yes_token, yes_bid, size))
    if 0 < no_bid and skew_frac > -1.0 and (no_mid - no_bid) <= band + 1e-9:
        quotes.append(Quote(market.no_token, no_bid, size))
    return quotes


def reconcile_quotes(current: list[Quote], desired: list[Quote],
                     move_cents: float, size_tol: float = 0.10) -> list[Quote]:
    """Merge desired quotes with the resting set, per side."""
    cur = {q.token_id: q for q in current}
    final = []
    for d in desired:
        c = cur.get(d.token_id)
        if (c is not None
                and abs(c.price - d.price) * 100 < move_cents
                and abs(c.size - d.size) <= size_tol * d.size):
            final.append(c)
        else:
            final.append(d)
    return final


def _safety_factor(market: Market, markout_cents: float | None, cfg: dict) -> float:
    """Toxicity discount in (0, 1]: 1.0 = benign, lower = more adverse-selection
    risk. Folds two bounded signals into per-market depth so the capital tiers
    don't size up equally into toxic books:

      * turnover (24h volume / book liquidity) — fast/informed flow proxy that is
        always available from the scan,
      * realized markout (cents, negative = we got picked off) once the market
        has accumulated fills; ignored while benign or unsampled.

    ``penalty = turnover_w·turnover + markout_w·max(0, -markout_cents)`` and the
    factor is ``1/(1+penalty)``. Setting both weights to 0 disables it.
    """
    q = cfg["quoting"]
    turn_w = float(q.get("size_turnover_penalty", 0.0))
    mk_w = float(q.get("size_markout_penalty", 0.0))
    turnover = market.volume_24h / max(market.liquidity, 100.0)
    penalty = turn_w * turnover
    if markout_cents is not None and markout_cents < 0:
        penalty += mk_w * (-markout_cents)
    return 1.0 / (1.0 + max(0.0, penalty))


def compute_size_factors(
    markets: list[Market],
    books: dict,
    open_quotes_fn,
    cfg: dict,
    markouts=None,
) -> dict[str, float]:
    """Per-market size multiplier from RISK-ADJUSTED reward $/day per $ committed.

    The reward term is estimated capture per dollar committed (pool × our share
    ÷ capital). It is then discounted by ``_safety_factor`` (turnover + realized
    markout) so the depth tiers put MORE size into deep/slow markets and LESS
    into thin/toxic ones. Factors are normalized to mean 1.0 and clamped, so this
    REALLOCATES depth across the held markets — it does not change the absolute,
    tier-controlled size. Pass ``markouts`` (a MarkoutTracker) to enable the
    realized-markout term; without it only the turnover proxy applies.
    """
    raw: dict[str, float] = {}
    for m in markets:
        yes_book = books.get(m.yes_token)
        no_book = books.get(m.no_token)
        if yes_book is None or no_book is None:
            raw[m.condition_id] = 1.0
            continue
        share = estimate_reward_share(m, yes_book, no_book, open_quotes_fn(m))
        capital = max(m.min_size, 1.0)
        reward = (m.daily_pool * share) / capital if capital > 0 else 0.0
        markout_cents = None
        if markouts is not None:
            mo = markouts.market_avg(m.condition_id)
            if mo is not None:
                markout_cents = mo * 100.0
        raw[m.condition_id] = reward * _safety_factor(m, markout_cents, cfg)
    if not raw:
        return {}
    avg = sum(raw.values()) / len(raw)
    if avg <= 0:
        return {cid: 1.0 for cid in raw}
    return {
        cid: max(0.5, min(2.0, v / avg))
        for cid, v in raw.items()
    }


# ---------------------------------------------------------------- rewards

def _order_score(spread_cents: float, max_spread_cents: float, size: float) -> float:
    """Polymarket scoring: S = ((v - s) / v)^2 * size, zero outside the band."""
    if spread_cents > max_spread_cents or max_spread_cents <= 0:
        return 0.0
    return ((max_spread_cents - spread_cents) / max_spread_cents) ** 2 * size


def _two_sided_q(q_one: float, q_two: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q_one, q_two), max(q_one / SINGLE_SIDED_DIVISOR, q_two / SINGLE_SIDED_DIVISOR))
    return min(q_one, q_two)


def estimate_reward_share(
    market: Market,
    yes_book: Book,
    no_book: Book,
    our_quotes: list[Quote],
) -> float:
    """Approximate our share of this market's reward pool right now."""
    mid = yes_book.mid
    if mid is None:
        return 0.0
    v = market.max_spread_cents
    band = v / 100.0

    ours_yes = sum(
        _order_score(abs(q.price - mid) * 100, v, q.size)
        for q in our_quotes if q.token_id == market.yes_token
    )
    no_mid = 1.0 - mid
    ours_no = sum(
        _order_score(abs(q.price - no_mid) * 100, v, q.size)
        for q in our_quotes if q.token_id == market.no_token
    )
    our_q = _two_sided_q(ours_yes, ours_no, mid)
    if our_q <= 0:
        return 0.0

    book_yes = sum(
        _order_score(abs(p - mid) * 100, v, s)
        for p, s in yes_book.depth_within(mid, band, "bid")
    )
    book_no = sum(
        _order_score(abs(p - no_mid) * 100, v, s)
        for p, s in no_book.depth_within(no_mid, band, "bid")
    )
    competitor_q = _two_sided_q(book_yes, book_no, mid)

    return our_q / (our_q + competitor_q) if (our_q + competitor_q) > 0 else 0.0
