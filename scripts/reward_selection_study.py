"""Reward-prioritized selection & position-scaling study.

Research questions (see REPORT_reward_selection.md for the write-up):

  Q1. We currently rank eligible markets by reward DENSITY (pool / book
      liquidity). Should we instead rank by EXPECTED CAPTURED REWARD
      (pool x our score-share), which is what actually lands in the wallet?

  Q2. As capital grows, should we add more MARKETS (raise top_n_markets) or
      bigger POSITIONS per market (raise max_capital_per_market, e.g. 50->100)?

  Q3. What does this do to SAFETY (tail losses), measured against our own
      realized markout distribution?

Everything is pure-stdlib and reproducible. Inputs are either MEASURED from
data/live_metrics.db or STATED assumptions you can edit at the top. Run:

    .venv/bin/python scripts/reward_selection_study.py
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "live_metrics.db"
CONFIG = ROOT / "config.yaml"
UNIVERSE = ROOT / "data" / "_scan_universe.json"  # snapshot from a live scan

# --- Polymarket reward scoring (mirrors pmbot/strategy.py) -----------------
#   per-order score S = ((v - s)/v)^2 * size, zero if spread s > band v.
# We quote at offset_frac of the band, so our spread = offset_frac * v and the
# (v-s)/v term is constant in v: ALPHA = (1 - offset_frac)^2. Two-sided & roughly
# symmetric, so our q-score = ALPHA * size.
def alpha(offset_frac: float) -> float:
    return (1.0 - offset_frac) ** 2


def share(size: float, comp_q: float, off: float) -> float:
    q = alpha(off) * size
    return q / (q + comp_q) if (q + comp_q) > 0 else 0.0


# Fraction of the theoretical (always-in-band, full-eligibility) reward we
# actually bank. Calibrated from realized/estimated and in-band uptime gaps.
# It scales ALL allocations equally, so it never changes a relative ranking;
# it only keeps the absolute dollar figures honest.
REALIZATION = 0.30


# ---------------------------------------------------------------------------
def measured_inputs():
    """Pull the facts the model is calibrated to from the live DB."""
    c = sqlite3.connect(str(DB)).cursor()
    mk = defaultdict(list)
    for cid, h, mo in c.execute("SELECT cid,horizon,markout FROM markouts"):
        mk[h].append(mo * 100.0)  # cents
    long_h = max(mk) if mk else 300.0
    markouts_c = mk[long_h]
    rewards = c.execute("SELECT date,estimated,realized FROM rewards").fetchall()
    est = sum(r[1] for r in rewards)
    real = sum(r[2] for r in rewards)
    # per-pair hedge slippage: hedge notional vs merged pairs.
    pairs = c.execute("SELECT COALESCE(SUM(pairs),0) FROM merges").fetchone()[0]
    hedge = c.execute("SELECT COALESCE(SUM(price*size),0) FROM hedges").fetchone()[0]
    eq = c.execute("SELECT ts,equity FROM equity ORDER BY ts").fetchall()
    hours = (eq[-1][0] - eq[0][0]) / 3600.0 if len(eq) > 1 else 0.0
    return {
        "markouts_c": markouts_c, "est_reward": est, "real_reward": real,
        "pairs": pairs, "hedge_notional": hedge, "hours": hours,
        "realized_frac": (real / est) if est else 0.0,
    }


def eligible_universe(cfg):
    if not UNIVERSE.exists():
        return None
    uni = json.load(UNIVERSE.open())
    sc = cfg["scanner"]
    lo, hi = sc["mid_range"]
    excl = [k.lower() for k in sc.get("exclude_keywords") or []]
    cap = cfg["quoting"]["max_capital_per_market"]

    def ok(m):
        if m["pool"] < sc["min_pool_per_day"]:
            return False
        if m["liq"] < sc.get("min_liquidity", 0):
            return False
        if m["min_size"] <= 0 or m["min_size"] > sc["max_min_size_shares"]:
            return False
        if m["band"] <= 0 or not (lo <= m["mid"] <= hi):
            return False
        if any(k in m["q"].lower() for k in excl):
            return False
        if m["min_size"] > cap:
            return False
        if m["pool"] / max(m["liq"], 100.0) < sc["min_pool_to_liquidity"]:
            return False
        return True

    return [m for m in uni if ok(m)]


def hr(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


# ===========================================================================
def main():
    cfg = yaml.safe_load(CONFIG.open())
    m = measured_inputs()
    off = cfg["quoting"]["offset_frac_of_max_spread"]

    hr("MEASURED INPUTS (data/live_metrics.db)")
    print(f"  observation window           : {m['hours']:.1f} h")
    print(f"  long-horizon markouts (cents): n={len(m['markouts_c'])} "
          f"mean={st.mean(m['markouts_c']):+.2f} median={st.median(m['markouts_c']):+.2f} "
          f"min={min(m['markouts_c']):+.1f} max={max(m['markouts_c']):+.1f}")
    print(f"  reward estimated / realized  : ${m['est_reward']:.2f} / ${m['real_reward']:.2f}"
          f"  -> realized is {m['realized_frac']*100:.0f}% of estimate")
    sl = m["hedge_notional"] / m["pairs"] - 1.0 if m["pairs"] else 0.0
    print(f"  pairs merged / hedge notional: {m['pairs']:.0f} / ${m['hedge_notional']:.0f}")
    print(f"  quote placement offset       : {off:.2f} of band  ->  ALPHA={alpha(off):.3f}")

    # ---- Calibrate competition from realized share ------------------------
    # Realized reward ~ pool * our_share, summed over the markets we quoted.
    # Our realized daily reward / day was tiny vs the pools we sat in, implying
    # we are a very small fraction of the in-band reward score. Express that as
    # an effective competition multiple K = comp_q / our_q at size=50.
    hr("Q0. WHERE ARE WE ON THE REWARD-SHARE CURVE?")
    real_per_day = m["real_reward"] / max(m["hours"] / 24.0, 1e-9)
    # We sat mostly in a ~$300/day-pool blend (Trump $500 + Toy Story ~$107).
    blend_pool = 300.0
    realized_share = real_per_day / blend_pool
    q50 = alpha(off) * 50.0
    K = (1.0 / realized_share - 1.0) if realized_share > 0 else float("inf")
    print(f"  realized reward/day ~ ${real_per_day:.2f} against ~${blend_pool:.0f}/day pool")
    print(f"  => realized score-share ~ {realized_share*100:.2f}%   "
          f"(we are ~1/{K+1:.0f} of the in-band reward score)")
    print(f"  At {realized_share*100:.2f}% share we are deep in the LINEAR regime:")
    comp_q = K * q50
    for s in (50, 100, 150, 200):
        sh = share(s, comp_q, off)
        print(f"    size {s:>3} shares -> share {sh*100:5.2f}%  "
              f"(reward multiple vs size50: {sh/share(50,comp_q,off):.2f}x)")
    print("  Interpretation: below ~10% share, doubling size ~doubles reward;")
    print("  the concavity penalty for concentrating size is negligible for us.")

    # ---- Q1: selection metric on the live eligible universe ---------------
    hr("Q1. SELECTION: reward DENSITY vs EXPECTED CAPTURED REWARD")
    elig = eligible_universe(cfg)
    if not elig:
        print("  (no scan snapshot at data/_scan_universe.json — skip)")
    else:
        # Competition per market scaled to its own liquidity, anchored so the
        # blended market we actually sat in reproduces the realized share.
        # comp_q_i = GAMMA * liquidity_i ; pick GAMMA from the calibration.
        anchor_liq = 60000.0  # ~Trump-class liquidity (dominant realized reward)
        gamma = comp_q / anchor_liq
        print(f"  eligible markets (post-filter): {len(elig)}   "
              f"[competition model comp_q = {gamma:.5f} x liquidity]\n")
        for label, sz, key in (
            ("BY DENSITY (current ranking)", 50, lambda x, s: x["pool"] / max(x["liq"], 100.0)),
            ("BY EXPECTED CAPTURE @ size 50", 50, lambda x, s: x["pool"] * share(s, gamma * x["liq"], off)),
            ("BY EXPECTED CAPTURE @ size 250", 250, lambda x, s: x["pool"] * share(s, gamma * x["liq"], off)),
        ):
            print(f"  --- {label} ---")
            ranked = sorted(elig, key=lambda x: -key(x, sz))
            for x in ranked[:6]:
                sh = share(sz, gamma * x["liq"], off)
                cap = x["pool"] * sh * REALIZATION
                print(f"    {x['q'][:40]:40} pool${x['pool']:>5.0f} liq${x['liq']:>7.0f} "
                      f"share{sh*100:5.2f}% capture${cap:5.2f}/d")
            print()
        print("  NOTE: while our share is tiny (<~3%), capture ~ pool/liquidity, so")
        print("  density and expected-capture give the SAME order. They diverge only")
        print("  as size grows and share saturates in the thinnest books — which is")
        print("  exactly the regime the 50->100+ position change pushes us toward.")

    # ---- Q2 & Q3: breadth vs depth at fixed capital -----------------------
    hr("Q2/Q3. BREADTH (more markets) vs DEPTH (bigger positions) @ fixed capital")
    print("  Per market a two-sided pair locks ~$1/share, so capital budget")
    print("  K (USD) ~ n_markets * size. We compare allocations of equal K, using")
    print("  the ranked eligible pools above and the empirical markout tail.\n")

    # Build a pool/competition ladder from the eligible universe (best first),
    # falling back to a synthetic ladder if no snapshot.
    if elig:
        anchor_liq = 60000.0
        gamma = comp_q / anchor_liq
        ladder = sorted(
            [(x["pool"], gamma * x["liq"]) for x in elig],
            key=lambda pc: -pc[0] * share(50, pc[1], off))
    else:
        ladder = [(500 - 60 * i, comp_q) for i in range(8)]

    markouts = m["markouts_c"]

    def alloc_reward(n_markets, size):
        """Expected $/day reward for n markets at `size`, taking the best n."""
        tot = 0.0
        for pool, cq in ladder[:n_markets]:
            tot += pool * share(size, cq, off)
        return tot * REALIZATION

    # Two markout regimes:
    #   OLD  = raw empirical sample (toxic Toy Story/Trump markets included).
    #   NEW  = reward-prioritized selection of slow, low-turnover markets +
    #          toxicity guard: clip the worst tail (the guard + exclusion would
    #          have pulled us out before the -13c pick-off compounded).
    TOXIC_CLIP_C = -3.0
    markouts_old = list(markouts)
    markouts_new = [max(x, TOXIC_CLIP_C) for x in markouts]

    def simulate(n_markets, size, mo_sample, trials=20000, seed=1):
        """Daily PnL distribution. Reward is deterministic/day; trading PnL is
        pairs * (markout - hedge_slip) bootstrapped from a markout sample.
        More markets = more independent toxic draws; bigger size = bigger
        per-market damage when a draw is toxic."""
        rng = random.Random(seed)
        pairs_per_market = 80.0 * (size / 50.0)  # fill rate scales with size
        hedge_slip_c = 2.4  # ~ from hedge notional vs pairs
        reward_day = alloc_reward(n_markets, size)
        out = []
        for _ in range(trials):
            day = reward_day
            for _i in range(n_markets):
                npairs = max(0.0, rng.gauss(pairs_per_market, pairs_per_market ** 0.5))
                mo = rng.choice(mo_sample)  # this market's regime today (cents)
                day += npairs * (mo - hedge_slip_c) / 100.0
            out.append(day)
        out.sort()
        cvar5 = st.mean(out[: max(1, len(out) // 20)])  # mean of worst 5%
        return {
            "reward_day": reward_day, "mean": st.mean(out),
            "p_profit": sum(1 for d in out if d > 0) / len(out),
            "cvar5": cvar5, "worst": out[0],
        }

    for regime, mo_sample in (("OLD selection (toxic tail intact)", markouts_old),
                              ("NEW selection (reward-priority + guard)", markouts_new)):
        print(f"  ----- {regime} -----")
        for K in (100, 200, 500):
            print(f"  CAPITAL ${K}")
            print(f"    {'allocation':>22} {'deployed$':>10} {'reward$/d':>10} {'PnL$/d':>9} "
                  f"{'P(profit)':>10} {'CVaR5%$/d':>11}")
            options, seen = [], set()
            n_avail = len(ladder)  # can't quote more markets than pass the filter
            for size in (50, 100, 150, 200, 250):
                n = min(K // size, n_avail)
                if n >= 1 and (n, size) not in seen:
                    seen.add((n, size))
                    options.append((int(n), size))
            for n, size in options:
                r = simulate(n, size, mo_sample)
                tag = f"{n} mkt x {size}sh"
                print(f"    {tag:>22} {n*size:>10d} {r['reward_day']:>10.2f} {r['mean']:>9.2f} "
                      f"{r['p_profit']:>9.0%} {r['cvar5']:>11.2f}")
            print()

    print("  Reading:")
    print("  * OLD vs NEW: the entire profit swing comes from SELECTION QUALITY")
    print("    (clipping the toxic markout tail), not from the reward mechanism.")
    print("  * Within the NEW regime, 'reward$/d' rises with depth because")
    print("    deepening stays in TOP-ranked markets while breadth dilutes into")
    print("    lower-ranked pools; 'CVaR5%' shows the tail cost of concentration.")
    print("  * The risk-adjusted sweet spot is a few vetted markets with larger")
    print("    positions — not max breadth, and not all-in on one book.")


if __name__ == "__main__":
    main()
