"""Per-tier position-sizing analysis ("how many shares per market, per tier").

The result is derived from first principles and validated against measured
economics. Three layers, each printed below:

  1. SIZE is bounded by two things — COLLATERAL (how much we can rest) and the
     RISK BUDGET (how much one market may lose). We show which one binds.
  2. Every risk knob that must move WITH size is co-scaled: force-pair trigger,
     inventory cap, theme cap, daily-loss and hard-kill limits.
  3. Because reward and trading PnL both scale ~linearly with deployed capital
     (we are tiny in every pool), SIZE is a pure MULTIPLIER on the per-pair
     edge. So the analysis ends on the edge itself: it is currently near
     break-even, which gates how fast we should scale.

Pure stdlib. Run:  .venv/bin/python scripts/tier_sizing.py
"""

from __future__ import annotations

import sqlite3
import statistics as st
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "live_metrics.db"
CONFIG = ROOT / "config.yaml"

# --------------------------- design rules (edit me) ------------------------
PHI = 0.65            # collateral utilisation (keep ~35% free for fills/hedge)
MID = 0.50            # representative mid (mid_range is 0.25..0.75)
MIN_FLOOR = 50        # Polymarket reward min_incentive_size on eligible markets
DAILY_LOSS_FRAC = 0.08
HARD_KILL_FRAC = 0.20
VOL_GUARD_C = 3.0     # quotes pull after a 3c adverse move (guards.vol_max_move)
HEDGE_SLIP_C = 2.36   # measured forced-hedge slippage (cents/share)
NEW_SLIP_MULT = 0.50  # wider quotes in clean markets assemble pairs nearer $1
TOXIC_CLIP_C = -5.0   # toxicity guard + exclusion clip the worst per-pair markout
PAIRS_PER_MKT_50 = 80.0  # ~pairs/market/day at size 50 (149 pairs / ~1.9 mkt-days)


def round_to(x, step):
    return int(round(x / step) * step)


def measured():
    c = sqlite3.connect(str(DB)).cursor()
    rows = c.execute("SELECT horizon,markout FROM markouts").fetchall()
    long_h = max(h for h, _ in rows) if rows else 300.0
    mk = [m * 100 for h, m in rows if h == long_h]
    est, real = c.execute("SELECT COALESCE(SUM(estimated),0),COALESCE(SUM(realized),0) "
                          "FROM rewards").fetchone()
    pairs = c.execute("SELECT COALESCE(SUM(pairs),0) FROM merges").fetchone()[0]
    return mk, est, real, pairs


def hr(t):
    print("\n" + "=" * 82 + f"\n{t}\n" + "=" * 82)


def main():
    cfg = yaml.safe_load(CONFIG.open())
    mk, est, real, pairs = measured()
    mk_old_mean = st.mean(mk)
    mk_new_mean = st.mean([max(x, TOXIC_CLIP_C) for x in mk])
    reward_per_pair_realized = (real / pairs * 100) if pairs else 0.0  # cents/pair

    # Depth-first ladder: low market count, scale SIZE. (Edit to taste.)
    tiers = [("T0", 100, 2), ("T1", 250, 2), ("T2", 750, 3),
             ("T3", 2000, 3), ("T4", 5000, 4)]

    hr("STEP 1 — what bounds SIZE: collateral vs risk budget")
    print(f"  rule A (collateral): N x size x ~$1/share <= {PHI:.0%} of equity")
    print(f"  rule B (risk budget): one market's worst NORMAL day <= daily loss/N")
    print(f"     worst normal day/market ~ unpaired_at_flatten x {VOL_GUARD_C:.0f}c vol-guard move\n")
    print(f"  {'tier':4}{'equity':>7}{'mkts':>5}{'collat-cap':>11}{'risk-cap':>9}"
          f"{'-> SIZE':>9}{'binds':>8}")
    sizes = {}
    for name, E, N in tiers:
        s_collat = PHI * E / N
        # risk cap: choose size so a one-sided run to the force-pair trigger,
        # then a full vol-guard move, costs <= the per-market daily budget.
        daily = DAILY_LOSS_FRAC * E
        # flatten trigger ~ 0.6 fill; unpaired shares at trigger ~ 0.6*size.
        # loss at guard = 0.6*size*VOL_GUARD_C/100 <= daily/N  ->  size <= ...
        s_risk = (daily / N) / (0.6 * VOL_GUARD_C / 100.0)
        s = max(MIN_FLOOR, round_to(min(s_collat, s_risk), 25))
        binds = "collat" if s_collat < s_risk else "risk"
        if s == MIN_FLOOR and MIN_FLOOR > min(s_collat, s_risk):
            binds = "FLOOR"
        sizes[name] = (E, N, s)
        print(f"  {name:4}{E:>7}{N:>5}{s_collat:>11.0f}{s_risk:>9.0f}{s:>9}{binds:>8}")

    hr("STEP 2 — recommended SIZE and the risk knobs that scale with it")
    print(f"  {'tier':4}{'equity':>7}{'mkts':>5}{'SIZE':>6}{'max_cap$':>9}"
          f"{'flatten$':>9}{'max_inv$':>9}{'theme$':>8}{'daily$':>7}{'kill$':>6}")
    knobs = {}
    for name in sizes:
        E, N, s = sizes[name]
        max_cap = s                                   # $ cap ~ shares
        flatten = round(0.6 * s * MID)                # force-pair trigger
        max_inv = round(1.5 * s * MID)                # net-exposure soft cap
        theme = max_inv                               # correlated-group cap
        daily = round_to(DAILY_LOSS_FRAC * E, 1)
        kill = round_to(HARD_KILL_FRAC * E, 1)
        knobs[name] = (max_cap, flatten, max_inv, theme, daily, kill)
        print(f"  {name:4}{E:>7}{N:>5}{s:>6}{max_cap:>9}{flatten:>9}"
              f"{max_inv:>9}{theme:>8}{daily:>7}{kill:>6}")

    hr("STEP 3 — tail checks (which control covers which failure)")
    for name in sizes:
        E, N, s = sizes[name]
        max_cap, flatten, max_inv, theme, daily, kill = knobs[name]
        normal = 0.6 * s * VOL_GUARD_C / 100.0        # one-sided run, guard pulls
        gap_1mkt = max_inv                            # a single market resolves wrong
        gap_all = N * max_inv
        print(f"  {name}: normal bad day/mkt ~${normal:.1f} ({normal/daily:.0%} of daily) "
              f"| 1-mkt GAP ~${gap_1mkt} ({gap_1mkt/kill:.0%} of kill) "
              f"| all-mkt GAP ${gap_all} ({'OVER' if gap_all>kill else 'ok'} kill)")
    print("\n  * Normal bleed is held well under the daily limit by the force-pair")
    print("    trigger + 3c volatility guard — the loss limits cover accumulation.")
    print("  * An instant binary GAP can exceed the daily limit; nothing dollar-based")
    print("    stops it. Gap safety comes from SELECTION (no news/event/near-resolution")
    print("    markets) + the exit window — not from sizing. Hard-kill is the backstop.")

    hr("STEP 4 — SIZE is a multiplier on the per-pair EDGE (the real gate)")
    edge_old = mk_old_mean - HEDGE_SLIP_C
    edge_new_trade = mk_new_mean - HEDGE_SLIP_C * NEW_SLIP_MULT
    print(f"  measured long-horizon markout: OLD mean {mk_old_mean:+.2f}c  "
          f"NEW (toxic-clipped) {mk_new_mean:+.2f}c")
    print(f"  trading edge/pair:  OLD {edge_old:+.2f}c   NEW {edge_new_trade:+.2f}c "
          f"(wider quotes halve hedge slip)")
    print(f"  reward/pair: realized {reward_per_pair_realized:.2f}c  "
          f"(est {est/pairs*100:.2f}c) — rises with better pools + uptime\n")
    for rname, rwd in (("pessimistic", 0.5), ("base", 1.5), ("optimistic", 3.0)):
        net = edge_new_trade + rwd
        print(f"  reward {rwd:.1f}c/pair ({rname:11}) -> NET edge {net:+.2f}c/pair "
              f"-> {'PROFIT, scale up' if net > 0 else 'LOSS, do NOT scale'}")
    be = -edge_new_trade
    print(f"\n  break-even reward yield (NEW regime): ~{be:.2f}c/pair.")
    print("  Daily PnL ~ N x 80 x (size/50) x net_edge/100. With net_edge near zero,")
    print("  scaling SIZE scales the OUTCOME in whichever direction the edge sits —")
    print("  so each size step must be GATED on realized per-pair PnL staying > 0.")


if __name__ == "__main__":
    main()
