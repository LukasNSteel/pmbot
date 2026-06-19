"""Probe Polymarket realized-rewards endpoints to confirm the correct call+shape.

Read-only: derives existing API creds and issues GET /rewards/user[/total] for a
few recent UTC dates via the official client methods, dumping raw responses so
we parse the right field. Run:  .venv/bin/python scripts/rewards_probe.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import yaml
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    from py_clob_client_v2 import ClobClient

    cfg = yaml.safe_load(open("config.yaml"))
    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ.get("POLYMARKET_FUNDER")
    sig = int(cfg["live"]["signature_type"])
    client = ClobClient("https://clob.polymarket.com", chain_id=137, key=pk,
                        signature_type=sig, funder=funder)
    client.set_api_creds(client.create_or_derive_api_key())
    print(f"client ready (sig_type={sig}, funder={funder})\n")

    today = datetime.now(timezone.utc).date()
    for d in range(0, 5):
        date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        print("=" * 60, "\nDATE", date)
        try:
            total = client.get_total_earnings_for_user_for_day(date)
            print("  get_total_earnings_for_user_for_day ->")
            print("   ", json.dumps(total, default=str)[:400])
        except Exception as e:  # noqa: BLE001
            print("  total ERR:", type(e).__name__, e)
        try:
            per = client.get_earnings_for_user_for_day(date)
            print(f"  get_earnings_for_user_for_day -> {len(per)} rows")
            if per:
                print("    sample row:", json.dumps(per[0], default=str)[:400])
        except Exception as e:  # noqa: BLE001
            print("  per-market ERR:", type(e).__name__, e)


if __name__ == "__main__":
    main()
