"""Backfill realized liquidity rewards into the metrics DB from the CLOB.

The old fetcher recorded $0 for days you were actually paid (bad request
signing). This re-pulls the authoritative per-day totals and writes them into
the configured metrics DB so `report`/`backtest` reflect reality.

Safe to run while the bot is live (the DB uses a busy timeout), but running it
while the bot is stopped avoids any lock contention. Run:

    .venv/bin/python -m scripts.backfill_rewards --days 14
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running as a plain script (`python scripts/backfill_rewards.py`) in
# addition to module form (`python -m scripts.backfill_rewards`) by ensuring the
# repo root is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from dotenv import load_dotenv

from pmbot.metrics import MetricsStore


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="UTC days back to backfill")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    if cfg.get("mode") != "live":
        print("warning: mode is not 'live'; rewards are only meaningful for the live wallet")

    from py_clob_client_v2 import ClobClient

    pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not pk:
        raise SystemExit("POLYMARKET_PRIVATE_KEY missing from .env")
    funder = os.environ.get("POLYMARKET_FUNDER")
    sig = int(cfg["live"]["signature_type"])
    client = ClobClient("https://clob.polymarket.com", chain_id=137, key=pk,
                        signature_type=sig, funder=funder)
    client.set_api_creds(client.create_or_derive_api_key())

    m = cfg.get("metrics") or {}
    store = MetricsStore(m.get("db_path", "data/metrics.db"),
                         trades_log=m.get("trades_log"),
                         inception_date=m.get("inception_date"))
    results = store.backfill_realized_rewards(client, days=args.days)
    store.close()

    total = sum(results.values())
    print(f"backfilled {len(results)} days into {m.get('db_path')}:")
    for date in sorted(results):
        print(f"  {date}: ${results[date]:.2f}")
    print(f"  total realized: ${total:.2f}")


if __name__ == "__main__":
    main()
