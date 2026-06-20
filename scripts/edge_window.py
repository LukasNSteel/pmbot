"""Per-pair EDGE over rolling time windows (is the post-fix book +EV yet?).

Two views, side by side:

  1. CASH LEDGER (ground truth) — net trade P&L per merged pair straight from
     the logged cashflows (merges + sells - buys - fees), plus realized reward
     per pair. This is the honest +EV test; it matches Polymarket history.
  2. MARKOUT PROXY (tier_sizing.py STEP 4) — markout_mean - hedge_slip. Kept
     only for comparison; it was found to read ~1c/pair too rosy because the
     toxic-tail clip and a static slip constant hide real assembly cost.

    net_edge/pair = reward/pair + trade_edge/pair

Realized rewards are CLOB-finalized per UTC day, and bought pairs may merge
just outside a short window, so the shortest windows read low on BOTH views;
all-time is the reliable cut.

Run:  .venv/bin/python scripts/edge_window.py
"""

from __future__ import annotations

import sqlite3
import statistics as st
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "live_metrics.db"

HEDGE_SLIP_C = 2.36       # measured forced-hedge slippage (cents/share)
NEW_SLIP_MULT = 0.50      # wider quotes assemble pairs nearer $1
TOXIC_CLIP_C = -5.0       # toxicity guard clips worst per-pair markout

WINDOWS_H = [24, 48, 72, 168, None]  # None = all-time


def _dates_in(since_ts: float | None) -> set[str]:
    """UTC date strings touched by the window (for the daily rewards table)."""
    if since_ts is None:
        return set()
    out = set()
    t = since_ts
    now = time.time()
    while t <= now + 86400:
        out.add(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"))
        t += 86400
    return out


def window(conn: sqlite3.Connection, hours: float | None) -> dict:
    since = None if hours is None else time.time() - hours * 3600

    mk_rows = conn.execute(
        "SELECT horizon, markout FROM markouts"
        + ("" if since is None else " WHERE ts >= ?"),
        () if since is None else (since,),
    ).fetchall()
    long_h = max((h for h, _ in mk_rows), default=300.0)
    mk = [m * 100 for h, m in mk_rows if h == long_h]

    pairs = conn.execute(
        "SELECT COALESCE(SUM(pairs),0) FROM merges"
        + ("" if since is None else " WHERE ts >= ?"),
        () if since is None else (since,),
    ).fetchone()[0] or 0.0

    if since is None:
        real, est = conn.execute(
            "SELECT COALESCE(SUM(realized),0), COALESCE(SUM(estimated),0) FROM rewards"
        ).fetchone()
    else:
        dates = _dates_in(since)
        qmarks = ",".join("?" * len(dates))
        real, est = conn.execute(
            f"SELECT COALESCE(SUM(realized),0), COALESCE(SUM(estimated),0) "
            f"FROM rewards WHERE date IN ({qmarks})", tuple(dates)
        ).fetchone()

    # CASH LEDGER: net trade P&L per merged pair (cents).
    buys = conn.execute(
        "SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=0"
        + ("" if since is None else " AND ts >= ?"),
        () if since is None else (since,)).fetchone()[0] or 0.0
    sells = conn.execute(
        "SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=1"
        + ("" if since is None else " AND ts >= ?"),
        () if since is None else (since,)).fetchone()[0] or 0.0
    fees = conn.execute(
        "SELECT COALESCE(SUM(fee),0) FROM fills"
        + ("" if since is None else " WHERE ts >= ?"),
        () if since is None else (since,)).fetchone()[0] or 0.0
    trade_cash = pairs + sells - buys - fees  # merges = $1/pair
    trade_cash_cps = (trade_cash / pairs * 100) if pairs else 0.0

    mk_mean = st.mean(mk) if mk else 0.0
    mk_clipped = st.mean([max(x, TOXIC_CLIP_C) for x in mk]) if mk else 0.0
    trade_edge = mk_clipped - HEDGE_SLIP_C * NEW_SLIP_MULT
    rwd_real = (real / pairs * 100) if pairs else 0.0
    rwd_est = (est / pairs * 100) if pairs else 0.0
    return {
        "n_mk": len(mk), "pairs": pairs,
        "mk_mean": mk_mean, "mk_clipped": mk_clipped,
        "trade_edge": trade_edge,
        "trade_cash_cps": trade_cash_cps,
        "rwd_real": rwd_real, "rwd_est": rwd_est,
        "real": real or 0.0,
        "net_real": trade_edge + rwd_real,
        "net_est": trade_edge + rwd_est,
        "net_cash": trade_cash_cps + rwd_real,
    }


def main():
    conn = sqlite3.connect(str(DB))

    print("CASH LEDGER (ground truth) — net per merged pair, cents")
    hdr = (f"{'window':>9}{'pairs':>8}{'trade(cash)':>13}{'reward':>9}{'NET(cash)':>11}")
    print(hdr)
    print("-" * len(hdr))
    for h in WINDOWS_H:
        w = window(conn, h)
        label = "all" if h is None else f"{int(h)}h"
        print(f"{label:>9}{w['pairs']:>8.0f}{w['trade_cash_cps']:>+12.2f}c"
              f"{w['rwd_real']:>+8.2f}c{w['net_cash']:>+10.2f}c")
    print("  NET(cash) > 0 = +EV per pair. Short windows read low: bought pairs")
    print("  merge/redeem outside the window and rewards finalize daily. Trust 'all'.")

    print("\nMARKOUT PROXY (for comparison only — reads ~1c/pair too rosy)")
    hdr2 = (f"{'window':>9}{'mk_n':>6}{'markout':>9}{'clipped':>9}"
            f"{'trade':>8}{'rwd(real)':>11}{'NET(real)':>11}")
    print(hdr2)
    print("-" * len(hdr2))
    for h in WINDOWS_H:
        w = window(conn, h)
        label = "all" if h is None else f"{int(h)}h"
        print(f"{label:>9}{w['n_mk']:>6}{w['mk_mean']:>+8.2f}c{w['mk_clipped']:>+8.2f}c"
              f"{w['trade_edge']:>+7.2f}c{w['rwd_real']:>+10.2f}c{w['net_real']:>+10.2f}c")


if __name__ == "__main__":
    main()
