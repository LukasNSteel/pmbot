"""Market scanner: finds reward-paying markets worth quoting via the Gamma API."""

from __future__ import annotations

import json
import logging
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
    fee_bps: int = 0
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


def _fetch_fee_bps(token_id: str, cache: dict[str, int | None]) -> int | None:
    if token_id in cache:
        return cache[token_id]
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{CLOB_URL}/fee-rate", params={"token_id": token_id})
            resp.raise_for_status()
            fee = int(resp.json().get("base_fee") or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("fee-rate fetch failed for %s…; skipping market: %s",
                    token_id[:12], e)
        fee = None
    cache[token_id] = fee
    return fee


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


def scan(cfg: dict) -> list[Market]:
    """Filter and rank reward markets per scanner config. Returns best first."""
    sc = cfg["scanner"]
    lo, hi = sc["mid_range"]
    min_end = datetime.now(timezone.utc) + timedelta(hours=sc["min_hours_to_end"])
    exclude = [k.lower() for k in sc.get("exclude_keywords") or []]
    max_capital = cfg["quoting"]["max_capital_per_market"]
    fee_penalty = float(sc.get("fee_penalty_mult", 0.5))
    max_fee_bps = int(sc.get("max_fee_bps", 0))

    fee_cache: dict[str, int | None] = {}
    candidates = []
    for m in fetch_reward_markets():
        if m.daily_pool < sc["min_pool_per_day"]:
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
        fee_bps = _fetch_fee_bps(m.yes_token, fee_cache)
        if fee_bps is None:
            continue
        m.fee_bps = fee_bps
        if m.fee_bps > max_fee_bps:
            # At 1000bps a fill at mid 0.50 costs 5c/share — more than the
            # whole reward band. Fee markets are unquotable for this strategy.
            continue
        base_score = m.daily_pool / max(m.liquidity, 100.0)
        if m.fee_bps > 0:
            base_score *= max(0.1, 1.0 - m.fee_bps / 10000.0 * fee_penalty)
        m.score = base_score
        if m.score < sc["min_pool_to_liquidity"]:
            continue
        candidates.append(m)

    candidates.sort(key=lambda m: m.score, reverse=True)
    return candidates[: sc["top_n_markets"]]
