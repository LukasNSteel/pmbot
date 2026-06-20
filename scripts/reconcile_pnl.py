"""Ground-truth trading P&L from the cash ledger (vs the hedge_pnl ESTIMATE).

The bot's status line shows `hedge P&L (est)` from MetricsStore.hedge_pnl_totals,
which APPROXIMATES forced-hedge pairing loss (assumed basis, capped pairs). This
script instead reconciles the actual logged cashflows, the way the Polymarket
trade history does:

    trading P&L (realized) = merges($1/pair) + sells(exits) - buys - fees
    trading P&L (mark-to-mkt) = realized + current inventory value

Buys (maker reward quotes + taker hedges) are cash OUT; exits are cash IN; a
merge returns $1 per pair. Rewards and deposits are EXCLUDED (they aren't
trading P&L). This is the apples-to-apples number to compare against your
Polymarket history: sum(+/- trades) - deposits - rewards.

Run:  .venv/bin/python scripts/reconcile_pnl.py
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "live_metrics.db"


def reconcile(conn: sqlite3.Connection, since: float | None) -> dict:
    where = "" if since is None else " WHERE ts >= ?"
    args = () if since is None else (since,)

    buys = conn.execute(
        f"SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=0"
        + ("" if since is None else " AND ts >= ?"), args).fetchone()[0]
    sells = conn.execute(
        f"SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=1"
        + ("" if since is None else " AND ts >= ?"), args).fetchone()[0]
    fees = conn.execute(
        f"SELECT COALESCE(SUM(fee),0) FROM fills" + where, args).fetchone()[0]
    merge_pairs = conn.execute(
        f"SELECT COALESCE(SUM(pairs),0) FROM merges" + where, args).fetchone()[0]

    # Split buys into maker (reward quotes) and taker (forced hedges) for color.
    taker_buys = conn.execute(
        f"SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=0 AND taker=1"
        + ("" if since is None else " AND ts >= ?"), args).fetchone()[0]
    hedge_spend = conn.execute(
        f"SELECT COALESCE(SUM(price*size),0) FROM hedges" + where, args).fetchone()[0]

    realized = merge_pairs + sells - buys - fees
    return {
        "buys": buys, "taker_buys": taker_buys, "sells": sells,
        "fees": fees, "merge_pairs": merge_pairs, "hedge_spend": hedge_spend,
        "realized": realized,
    }


def main():
    conn = sqlite3.connect(str(DB))

    # Current inventory mark + equity (last sample) to turn realized cash into MTM.
    row = conn.execute(
        "SELECT equity, inventory_usd FROM equity ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    equity_now, inv_now = (row or (float("nan"), 0.0))

    rewards_real = conn.execute(
        "SELECT COALESCE(SUM(realized),0) FROM rewards").fetchone()[0]

    print(f"{'='*70}\nGROUND-TRUTH TRADING P&L (cash ledger)\n{'='*70}")
    for label, hours in (("last 24h", 24), ("last 72h", 72), ("all-time", None)):
        since = None if hours is None else time.time() - hours * 3600
        r = reconcile(conn, since)
        mtm = r["realized"] + (inv_now if hours is None else 0.0)
        print(f"\n--- {label} ---")
        print(f"  buys (cash out)        : -${r['buys']:.2f}  "
              f"(of which forced-hedge takers ${r['taker_buys']:.2f})")
        print(f"  exits/sells (cash in)  : +${r['sells']:.2f}")
        print(f"  merges (cash in, $1/pr): +${r['merge_pairs']:.2f}  "
              f"({r['merge_pairs']:.0f} pairs)")
        print(f"  fees                   : -${r['fees']:.2f}")
        print(f"  ---------------------------------------------")
        print(f"  REALIZED trading P&L   : ${r['realized']:+.2f}")
        if hours is None:
            print(f"  + current inventory mark: +${inv_now:.2f}")
            print(f"  MARK-TO-MARKET trading P&L: ${mtm:+.2f}")

    print(f"\n{'='*70}\nCROSS-CHECK vs the hedge_pnl ESTIMATE\n{'='*70}")
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from pmbot.metrics import MetricsStore
        ms = MetricsStore(db_path=str(DB))
        hp = ms.hedge_pnl_totals()
        print(f"  hedge_pnl_totals (est) : ${hp['pnl_total']:+.2f} total / "
              f"${hp['pnl_24h']:+.2f} 24h   [forced-hedge pairs only, est basis]")
    except Exception as e:  # noqa: BLE001
        print(f"  (could not load estimate: {e})")
    print(f"  realized rewards       : +${rewards_real:.2f}  (separate credit)")
    print(f"  equity now             : ${equity_now:.2f}  (inv ${inv_now:.2f})")
    print("\nThe REALIZED/MTM trading P&L above is the apples-to-apples match for")
    print("your Polymarket history: sum(+/- trades) - deposits - rewards.")
    print("The hedge_pnl estimate measures only forced-hedge pairing loss with an")
    print("assumed basis, so it will NOT equal the full trading P&L.")


if __name__ == "__main__":
    main()
