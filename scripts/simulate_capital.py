"""Forward Monte-Carlo simulation of expected PnL at $100 / $500 / $1,000.

This is NOT a replay (see scripts/backtest.py for that). It projects forward
using the per-pair economics we MEASURED on the non-event book, scaled by the
capital tier the controller would select. Every assumption is stated up front
and is adjustable — treat the output as a decision aid, not a promise. The
sample (one ~18h session, 5 long-horizon markout samples) is tiny, so the
spread of outcomes matters more than any single number.

Core measured facts (data/live_metrics.db, non-event markets only):
  * 149 pairs assembled, net -$6.80  ->  -4.56c per pair (avg pair cost $1.0456)
  * long-horizon markouts: [-13, 0, +0.5, +0.5, +1.0] cents
    (benign-to-positive except ONE toxic outlier that drove most of the loss)

Model per simulated day, per quoted market:
  pairs ~ Poisson(PAIRS_PER_MARKET_DAY)
  each pair's PnL(cents) = sampled_markout - HEDGE_SLIPPAGE_C   (+ reward yield)
  where the markout is bootstrap-sampled from the empirical pool.
The "new regime" (wide quotes + toxicity guard + tier inventory cap) (a) halves
hedge slippage — wider bids assemble pairs closer to $1.00 — and (b) clips the
toxic tail at TOXIC_CLIP_C (passive exit + inventory cap limit per-fill damage).

Run:  .venv/bin/python scripts/simulate_capital.py
"""

from __future__ import annotations

import random
import statistics
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"

# ----------------------------- measured / assumed inputs -------------------
EMPIRICAL_MARKOUTS_C = [-13.0, 0.0, 0.5, 0.5, 1.0]  # non-event, long horizon
HEDGE_SLIPPAGE_C = 2.36      # calibrated so old-regime mean PnL = -4.56c/pair
PAIRS_PER_MARKET_DAY = 100   # 149 pairs / ~1.5 market-days observed
DAYS = 30                    # projection horizon
TRIALS = 4000                # Monte-Carlo days per cell

# New-regime mitigations (wider quotes + guard + inventory cap).
NEW_SLIPPAGE_MULT = 0.5      # wider bids -> assemble nearer $1.00
TOXIC_CLIP_C = -5.0          # cap worst per-pair markout (passive exit/cap)

# Reward yield per pair (cents). Observed: Trump $500-pool ~0.8c/pair,
# Toy Story ~0.13c/pair. Better pools via Option C ranking sit higher.
REWARD_SCENARIOS = {"pessimistic": 0.2, "base": 1.0, "optimistic": 3.0}


def tier_for(equity: float, tiers: list[dict]) -> dict:
    chosen = tiers[0]
    for t in sorted(tiers, key=lambda x: x["min_equity_usd"]):
        if equity >= t["min_equity_usd"]:
            chosen = t
    return chosen


def _pair_pnl_moments(new_regime: bool, reward_c: float) -> tuple[float, float]:
    """Mean and variance (cents) of one pair's PnL under the chosen regime."""
    slip = HEDGE_SLIPPAGE_C * (NEW_SLIPPAGE_MULT if new_regime else 1.0)
    vals = []
    for mo in EMPIRICAL_MARKOUTS_C:
        if new_regime:
            mo = max(mo, TOXIC_CLIP_C)
        vals.append(mo - slip + reward_c)
    mean = statistics.mean(vals)
    var = statistics.pvariance(vals)
    return mean, var


def run_cell(markets: int, new_regime: bool, reward_c: float,
             daily_loss_limit: float, seed: int) -> dict:
    """Daily PnL via CLT: sum of (markets * Poisson(pairs)) iid pair PnLs.

    Count ~ Poisson(lam) contributes lam*E[x]^2 of extra variance (law of total
    variance), folded in below. Days are Normal draws, floored at the daily
    loss limit (quoting pauses once breached)."""
    import math
    mean_c, var_c = _pair_pnl_moments(new_regime, reward_c)
    lam = markets * PAIRS_PER_MARKET_DAY
    day_mean_c = lam * mean_c
    day_var_c = lam * var_c + lam * (mean_c ** 2)  # E[N]Var(x) + Var(N)E[x]^2
    day_sd_c = math.sqrt(day_var_c)
    rng = random.Random(seed)
    days = []
    for _ in range(TRIALS):
        pnl = rng.gauss(day_mean_c, day_sd_c) / 100.0
        days.append(max(pnl, -daily_loss_limit))
    mean = statistics.mean(days)
    p_profit = sum(1 for d in days if d > 0) / len(days)
    return {"daily_mean": mean, "monthly": mean * DAYS, "p_profit_day": p_profit}


def main():
    cfg = yaml.safe_load(CONFIG.open())
    tiers = cfg["controller"]["capital_tiers"]

    print("Assumptions:")
    print(f"  empirical markouts (c): {EMPIRICAL_MARKOUTS_C}   "
          f"hedge slippage {HEDGE_SLIPPAGE_C}c   pairs/market/day {PAIRS_PER_MARKET_DAY}")
    print(f"  new regime: slippage x{NEW_SLIPPAGE_MULT}, toxic markout clipped at {TOXIC_CLIP_C}c")
    print(f"  Monte-Carlo: {TRIALS} days/cell, {DAYS}-day projection\n")

    for equity in (100.0, 500.0, 1000.0):
        t = tier_for(equity, tiers)
        markets = int(t["top_n_markets"])
        dll = float(t["daily_loss_limit_usd"])
        print("=" * 78)
        print(f"CAPITAL ${equity:.0f}  ->  tier (min ${t['min_equity_usd']}): "
              f"{markets} market(s), daily-loss-limit ${dll:.0f}, "
              f"inv cap ${t['max_inventory_usd_per_market']:.0f}/mkt")
        print("=" * 78)
        print(f"{'regime':>10} {'reward/pair':>12} {'$/day':>9} {'$/month':>10} "
              f"{'P(profit/day)':>14} {'monthly %ROI':>13}")
        for regime_name, new_regime in (("OLD", False), ("NEW", True)):
            for sc_name, reward_c in REWARD_SCENARIOS.items():
                r = run_cell(markets, new_regime, reward_c, dll,
                             seed=hash((equity, regime_name, sc_name)) & 0xFFFF)
                roi = r["monthly"] / equity * 100
                print(f"{regime_name+'/'+sc_name:>10} {reward_c:>10.1f}c "
                      f"{r['daily_mean']:>9.2f} {r['monthly']:>10.2f} "
                      f"{r['p_profit_day']:>13.0%} {roi:>12.1f}%")
        # Breakeven reward yield under the new regime.
        be = _breakeven_reward(markets, dll)
        print(f"  breakeven reward yield (NEW regime): ~{be:.2f}c/pair "
              f"(below this, the book loses money)\n")


def _breakeven_reward(markets: int, dll: float) -> float:
    rng = random.Random(1)
    lo, hi = 0.0, 10.0
    for _ in range(40):
        mid = (lo + hi) / 2
        r = run_cell(markets, True, mid, dll, seed=7)
        if r["daily_mean"] > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


if __name__ == "__main__":
    main()
