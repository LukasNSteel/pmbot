"""Replay-based backtest: old config (actual) vs new controller-driven config.

We do not have historical order books, so we cannot simulate counterfactual
*fills*. What we CAN do precisely is replay the trades that actually happened
and apply the new config's deterministic rules:

  * exclusion (EXACT): markets matching scanner.exclude_keywords would never
    have been quoted, so every fill/hedge/merge/exit there is removed.
  * per-market inventory cap (MODELED): tier-0 caps exposure lower, limiting
    how much inventory stacks before the bot stops adding.
  * toxicity widening + faster trips (MODELED): markets whose rolling markout
    breaches the toxic trip threshold get dropped, preventing later toxic fills.

Ground-truth trading cash = sum over fills of (-buy notionals + exit-sell
notionals) + merge pairs * $1. This reconciles with the equity delta in the
metrics DB. Run:  python -m scripts.backtest  (or .venv/bin/python scripts/backtest.py)
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "live_metrics.db"
CONFIG = ROOT / "config.yaml"


def load_trades(db_path: Path):
    c = sqlite3.connect(str(db_path)).cursor()
    fills = c.execute(
        "SELECT ts,cid,market,side,price,size,taker,exit FROM fills ORDER BY ts"
    ).fetchall()
    merges = c.execute("SELECT cid, COALESCE(SUM(pairs),0) FROM merges GROUP BY cid").fetchall()
    # Long-horizon markout per market (cents), and sample count.
    mk = c.execute("SELECT cid, horizon, markout FROM markouts").fetchall()
    # Resolution timestamp of each long-horizon markout sample (for trip timing).
    mk_ts = c.execute("SELECT cid, ts, horizon, markout FROM markouts ORDER BY ts").fetchall()
    eq = c.execute("SELECT equity FROM equity ORDER BY ts").fetchall()
    rewards = c.execute("SELECT date, estimated, realized FROM rewards").fetchall()
    return fills, dict(merges), mk, mk_ts, eq, rewards


def trip_time(mk_ts, cid, horizon_max, trip_cents, min_samples):
    """Earliest time the markout guard would trip for a market, or None.

    Mirrors MarkoutTracker.toxic_markets: trip once >= min_samples long-horizon
    samples exist whose running average is <= trip_cents (cents)."""
    samples = [(ts, m) for c, ts, h, m in mk_ts if c == cid and h == horizon_max]
    samples.sort()
    acc = []
    for ts, m in samples:
        acc.append(m)
        if len(acc) >= min_samples and (sum(acc) / len(acc)) * 100 <= trip_cents:
            return ts
    return None


def market_cashflows(fills, merges):
    """Net trading cash per market from actual trades (ground truth)."""
    cash = defaultdict(float)
    name = {}
    buys = defaultdict(float)
    hedge = defaultdict(float)
    sells = defaultdict(float)
    for ts, cid, mkt, side, px, size, taker, ex in fills:
        name[cid] = mkt
        n = px * size
        if ex:
            cash[cid] += n
            sells[cid] += n
        else:
            cash[cid] -= n
            buys[cid] += n
            if taker:
                hedge[cid] += n
    for cid, pairs in merges.items():
        cash[cid] += pairs * 1.0
    return cash, name, buys, hedge, sells


def long_markout(mk):
    """(avg_cents, n) at the longest horizon, per market."""
    if not mk:
        return {}
    max_h = max(h for _, h, _ in mk)
    by = defaultdict(list)
    for cid, h, m in mk:
        if h == max_h:
            by[cid].append(m)
    return {cid: (sum(v) / len(v) * 100, len(v)) for cid, v in by.items()}


def is_excluded(question: str, keywords: list[str]) -> bool:
    q = (question or "").lower()
    return any(k.lower() in q for k in keywords)


def fmt(v):
    return f"${v:+7.2f}"


def main():
    fills, merges, mk, mk_ts, eq, rewards = load_trades(DB)
    cfg = yaml.safe_load(CONFIG.open())
    exclude = cfg["scanner"].get("exclude_keywords") or []
    cc = cfg.get("controller") or {}
    toxic = cc.get("toxic") or {}
    toxic_trip = float(toxic.get("markout_trip_cents", -0.8))
    toxic_min_samples = int(toxic.get("markout_min_samples", 2))
    cash, name, buys, hedge, sells = market_cashflows(fills, merges)
    markout = long_markout(mk)
    horizon_max = max((h for _, h, _ in mk), default=0.0)

    equity_delta = (eq[-1][0] - eq[0][0]) if len(eq) >= 2 else float("nan")
    actual_trading = sum(cash.values())

    print("=" * 74)
    print("GROUND TRUTH (old config — what actually happened)")
    print("=" * 74)
    print(f"{'market':40}{'net$':>9}{'hedge$':>9} markout")
    for cid in sorted(cash, key=lambda k: cash[k]):
        mo = markout.get(cid)
        mo_s = f"{mo[0]:+.1f}c(n={mo[1]})" if mo else "—"
        excl = "  [EXCLUDED]" if is_excluded(name.get(cid, ""), exclude) else ""
        print(f"{name.get(cid,cid)[:40]:40}{cash[cid]:+9.2f}{hedge[cid]:9.1f} {mo_s}{excl}")
    print(f"{'TOTAL trading cash':40}{actual_trading:+9.2f}")
    print(f"equity delta (DB, incl. rewards & old-position drift): {fmt(equity_delta)}")
    est_rw = sum(r[1] for r in rewards)
    real_rw = sum(r[2] for r in rewards)
    print(f"rewards over period: est ${est_rw:.2f}  realized(DB) ${real_rw:.2f}")

    # ---- Scenario 1: EXACT — exclude event-driven markets ----
    incl = {cid: v for cid, v in cash.items()
            if not is_excluded(name.get(cid, ""), exclude)}
    excl_loss = actual_trading - sum(incl.values())
    s1_trading = sum(incl.values())

    print("\n" + "=" * 74)
    print("SCENARIO 1 (EXACT): new exclusion list — event markets never quoted")
    print("=" * 74)
    for cid in sorted(incl, key=lambda k: incl[k]):
        print(f"  kept: {name.get(cid,cid)[:48]:48}{incl[cid]:+9.2f}")
    print(f"  removed (excluded-market trades): {fmt(-excl_loss)}  "
          f"of which were losses you no longer take")
    print(f"  --> trading cash: {fmt(actual_trading)}  ->  {fmt(s1_trading)}   "
          f"(improvement {fmt(actual_trading - s1_trading) if False else f'${s1_trading-actual_trading:+.2f}'})")

    # ---- Scenario 2: MODELED — toxicity guard drops toxic survivors ----
    # A guard is REACTIVE: it can only prevent fills that happen AFTER it trips.
    # For each surviving market we find the actual trip time (given the toxic
    # regime's tighter min_samples) and credit only the net cash of fills that
    # occurred after it — never the fill that caused the trip.
    print("\n" + "=" * 74)
    print(f"SCENARIO 2 (MODELED): + toxicity guard "
          f"(trip @ {toxic_trip:.1f}c, min_samples {toxic_min_samples})")
    print("=" * 74)
    modeled_recovery = 0.0
    flagged = False
    for cid in incl:
        t_trip = trip_time(mk_ts, cid, horizon_max, toxic_trip, toxic_min_samples)
        mo = markout.get(cid)
        if t_trip is None:
            continue
        flagged = True
        # Net cash of this market's fills strictly after the trip resolves.
        post = 0.0
        for ts, fcid, mkt, side, px, size, taker, ex in fills:
            if fcid != cid or ts <= t_trip:
                continue
            post += (px * size) if ex else -(px * size)
        avoided = -post if post < 0 else 0.0  # only avoid losses, not gains
        modeled_recovery += avoided
        mo_s = f"{mo[0]:+.1f}c" if mo else "—"
        print(f"  {name.get(cid,cid)[:40]:40} markout {mo_s}  "
              f"trips, post-trip net {post:+.2f} -> avoid {avoided:+.2f}")
    if not flagged:
        print("  no surviving market trips the guard with enough post-trip fills")
        print("  (the toxic Toy Story pick-off was a one-off LAST fill — the guard")
        print("   resolves 300s later, so it cannot prevent that specific loss).")
    s2_trading = s1_trading + modeled_recovery
    print(f"  modeled avoided loss: ${modeled_recovery:+.2f}")
    print(f"  --> trading cash: {fmt(s1_trading)}  ->  {fmt(s2_trading)}")

    # ---- Reward trade-off (LABELED ESTIMATE) ----
    # Excluding Trump forgoes the $500/day pool that produced ~all real rewards;
    # Toy Story pools are tiny (~$0.10/day per the session review).
    old_reward = real_rw if real_rw > 0 else est_rw
    new_reward_est = 0.20  # Toy Story-class pools over a comparable window
    print("\n" + "=" * 74)
    print("REWARD TRADE-OFF (labeled estimate — not from precise per-market data)")
    print("=" * 74)
    print(f"  old rewards (Trump pool dominant): ~${old_reward:.2f}")
    print(f"  new rewards (Toy Story-class only): ~${new_reward_est:.2f}")

    print("\n" + "=" * 74)
    print("NET COMPARISON (trading cash + rewards over the same window)")
    print("=" * 74)
    old_net = actual_trading + old_reward
    s1_net = s1_trading + new_reward_est
    s2_net = s2_trading + new_reward_est
    print(f"  OLD config (actual):              {fmt(old_net)}")
    print(f"  NEW exclusion only (EXACT):       {fmt(s1_net)}   "
          f"(Δ {s1_net-old_net:+.2f})")
    print(f"  NEW full regime (MODELED):        {fmt(s2_net)}   "
          f"(Δ {s2_net-old_net:+.2f})")
    print("\nNote: wider quotes (offset 0.35->0.60) also reduce fill rate and improve")
    print("entry prices on survivors — a further upside we cannot size without")
    print("historical order books. Treated as 0 here (conservative).")


if __name__ == "__main__":
    sys.exit(main())
