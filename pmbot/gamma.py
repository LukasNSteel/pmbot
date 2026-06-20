"""Market scanner: finds reward-paying markets worth quoting via the Gamma API."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("pmbot.gamma")

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL = "https://clob.polymarket.com"
SCAN_PAGES = 20
TIMEOUT = httpx.Timeout(15.0)


@dataclass
class Market:
    question: str
    condition_id: str
    yes_token: str
    no_token: str
    min_size: float
    max_spread_cents: float
    daily_pool: float
    liquidity: float
    volume_24h: float
    tick: float
    end_date: datetime | None
    neg_risk: bool
    event_id: str | None = None
    # Taker fee rate in bps (fd.r × 10000) and its exponent (fd.e), from the
    # CLOB clob-markets endpoint. Makers are never charged on Polymarket, so
    # this only prices crossing the spread on a merge/exit.
    fee_bps: int = 0
    fee_exponent: float = 1.0
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade: float | None = None
    score: float = field(default=0.0)

    @property
    def mid_hint(self) -> float:
        if self.best_bid is not None and self.best_ask is not None and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2
        return self.last_trade if self.last_trade is not None else 0.5


def _parse_market(m: dict) -> Market | None:
    rewards = m.get("clobRewards") or []
    daily_pool = sum(float(r.get("rewardsDailyRate") or 0) for r in rewards)
    if daily_pool <= 0:
        return None
    try:
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    if len(token_ids) != 2:
        return None
    end_raw = m.get("endDate")
    end_date = None
    if end_raw:
        try:
            end_date = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except ValueError:
            pass

    def _f(key: str) -> float | None:
        v = m.get(key)
        return float(v) if v is not None else None

    event_id = None
    for key in ("eventId", "eventSlug", "groupItemTitle"):
        if m.get(key):
            event_id = str(m[key])
            break
    if event_id is None and m.get("events"):
        ev = m["events"]
        if isinstance(ev, list) and ev:
            event_id = str(ev[0].get("id") or ev[0].get("slug") or "")

    return Market(
        question=m.get("question") or "",
        condition_id=m.get("conditionId") or "",
        yes_token=str(token_ids[0]),
        no_token=str(token_ids[1]),
        min_size=float(m.get("rewardsMinSize") or 0),
        max_spread_cents=float(m.get("rewardsMaxSpread") or 0),
        daily_pool=daily_pool,
        liquidity=float(m.get("liquidityNum") or 0),
        volume_24h=float(m.get("volume24hr") or 0),
        tick=float(m.get("orderPriceMinTickSize") or 0.01),
        end_date=end_date,
        neg_risk=bool(m.get("negRisk")),
        event_id=event_id or None,
        best_bid=_f("bestBid"),
        best_ask=_f("bestAsk"),
        last_trade=_f("lastTradePrice"),
    )


def _fetch_market_fees(
    condition_id: str, cache: dict[str, tuple[int, float] | None],
    attempts: int = 2, backoff: float = 0.5,
) -> tuple[int, float]:
    """Per-market TAKER fee from the CLOB clob-markets endpoint.

    Returns (taker_fee_bps, exponent). Polymarket charges only takers (fd.to),
    so this rate prices crossing the spread on a forced hedge or merge/exit —
    our resting maker quotes pay nothing regardless. We read fd.r directly
    because the legacy /fee-rate endpoint (and the mbf/tbf base-fee fields)
    report a flat 1000 that does not reflect the real fee rate.

    Robustness: the lookup is retried a few times, and on persistent failure it
    FAILS OPEN with (0 bps, 1.0) rather than dropping the market. Since we earn
    rewards purely as a maker (zero fee), a transient inability to read the
    taker fee must not cost us an otherwise-good market — that previously left
    a quoting slot empty for a full refresh cycle. The only thing we lose
    visibility into on failure is the taker cost of a rare forced hedge/exit;
    the ``max_fee_bps`` guard still applies whenever the rate is readable.
    """
    if condition_id in cache and cache[condition_id] is not None:
        return cache[condition_id]
    last_err: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{CLOB_URL}/clob-markets/{condition_id}")
                resp.raise_for_status()
                fd = resp.json().get("fd") or {}
            rate = float(fd.get("r") or 0.0)
            exponent = float(fd.get("e") or 1.0)
            result = (round(rate * 10000), exponent)
            cache[condition_id] = result
            return result
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i + 1 < attempts:
                time.sleep(backoff * (i + 1))
    log.warning("clob-markets fee fetch failed for %s… after %d tries (%s); "
                "assuming 0bps taker fee (makers pay no fee, so we still quote)",
                condition_id[:12], attempts, last_err)
    result = (0, 1.0)
    cache[condition_id] = result
    return result


def fetch_reward_markets() -> list[Market]:
    """Fetch active markets with a nonzero daily reward pool."""
    seen: set[str] = set()
    markets: list[Market] = []
    orderings = [
        ("rewardsDailyRate", "false"),
        ("volume24hr", "false"),
    ]
    with httpx.Client(timeout=TIMEOUT) as client:
        for order, ascending in orderings:
            for page in range(SCAN_PAGES):
                try:
                    resp = client.get(
                        GAMMA_URL,
                        params={
                            "active": "true",
                            "closed": "false",
                            "limit": 100,
                            "offset": page * 100,
                            "order": order,
                            "ascending": ascending,
                        },
                    )
                    resp.raise_for_status()
                    batch = resp.json()
                except Exception as e:  # noqa: BLE001
                    log.debug("gamma fetch failed (order=%s page=%d): %s", order, page, e)
                    break
                if not batch:
                    break
                for raw in batch:
                    parsed = _parse_market(raw)
                    if parsed and parsed.condition_id not in seen:
                        seen.add(parsed.condition_id)
                        markets.append(parsed)
    return markets


def scan(cfg: dict, exclude_cids: set[str] | None = None,
         full: bool = False) -> list[Market]:
    """Filter and rank reward markets per scanner config. Returns best first.

    `exclude_cids` skips specific markets before ranking, so the next-best
    eligible markets backfill the top_n slots — used to rotate out of a
    guard-tripped market into a fresh one instead of wasting the slot.

    `full=True` returns every eligible market (best first) instead of just the
    top_n slice, so the caller can run its own sticky selection (keep markets
    we are already quoting unless a candidate is materially better). Fee lookups
    already run for every market that clears the cheap filters, so returning the
    full list costs nothing extra.
    """
    sc = cfg["scanner"]
    skip_cids = exclude_cids or set()
    lo, hi = sc["mid_range"]
    min_end = datetime.now(timezone.utc) + timedelta(hours=sc["min_hours_to_end"])
    exclude = [k.lower() for k in sc.get("exclude_keywords") or []]
    max_capital = cfg["quoting"]["max_capital_per_market"]
    # Absolute book-liquidity floor (USD). The reward-density ranking
    # (pool ÷ liquidity) structurally prefers thin books, which then trip the
    # volatility / book-not-quotable guards and tank in-band uptime. This floor
    # drops books too shallow to two-side without getting picked off. 0 disables.
    min_liquidity = float(sc.get("min_liquidity", 0.0))
    fee_penalty = float(sc.get("fee_penalty_mult", 0.5))
    max_fee_bps = int(sc.get("max_fee_bps", 0))
    # Option C — reward-density ranking adjusted for toxicity & band room.
    # Eligibility still uses raw reward density (below), so these only re-order
    # preference among markets the static filters already accept (graceful
    # fallback: zero weights reproduce the old ranking exactly).
    turnover_w = float(sc.get("toxicity_turnover_penalty", 0.0))
    band_w = float(sc.get("band_room_bonus", 0.0))

    fee_cache: dict[str, tuple[int, float] | None] = {}
    candidates = []
    for m in fetch_reward_markets():
        if m.condition_id in skip_cids:
            continue
        if m.daily_pool < sc["min_pool_per_day"]:
            continue
        if m.liquidity < min_liquidity:
            continue
        if m.min_size <= 0 or m.min_size > sc["max_min_size_shares"]:
            continue
        if m.max_spread_cents <= 0:
            continue
        if not (lo <= m.mid_hint <= hi):
            continue
        if m.end_date and m.end_date < min_end:
            continue
        if any(k in m.question.lower() for k in exclude):
            continue
        if m.min_size * 1.0 > max_capital:
            continue
        # Fails open to (0, 1.0) on a persistent lookup error — a missing taker
        # fee never drops a market we can make on as a (zero-fee) maker.
        m.fee_bps, m.fee_exponent = _fetch_market_fees(m.condition_id, fee_cache)
        if m.fee_bps > max_fee_bps:
            # Guard against pathological fee rates. Makers pay no fee on
            # Polymarket, so our reward quotes are unaffected; this only caps
            # markets where the taker merge/exit cost would be extreme.
            continue
        density = m.daily_pool / max(m.liquidity, 100.0)
        if m.fee_bps > 0:
            # Slight down-rank: taker merges/exits cost more in fee markets.
            density *= max(0.1, 1.0 - m.fee_bps / 10000.0 * fee_penalty)
        # Eligibility gate is on raw reward density — unchanged behavior.
        if density < sc["min_pool_to_liquidity"]:
            continue
        # Toxicity proxy: high 24h turnover vs resting liquidity means fast,
        # often informed flow (the kind that picks off maker quotes). Penalize it.
        turnover = m.volume_24h / max(m.liquidity, 100.0)
        tox_mult = 1.0 / (1.0 + turnover_w * turnover)
        # A wider reward band leaves more room to quote inside it and still
        # assemble pairs near $1.00 — directly eases the adverse-selection math.
        band_mult = 1.0 + band_w * max(0.0, m.max_spread_cents - 1.0)
        m.score = density * tox_mult * band_mult
        candidates.append(m)

    candidates.sort(key=lambda m: m.score, reverse=True)
    if full:
        return candidates
    return candidates[: sc["top_n_markets"]]
