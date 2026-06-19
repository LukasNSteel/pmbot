"""Merge ALL whole YES+NO pairs the deposit wallet currently holds back to pUSD.

Reads real positions from the Data API, and for every condition with an
offsetting YES+NO pair, merges floor(min(yes,no)) pairs via the gasless relayer.
Recovers locked capital as spendable pUSD. Idempotent / safe to re-run.

Run:  .venv/bin/python -m scripts.merge_all
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict

import certifi
import httpx
import yaml
from dotenv import load_dotenv

from pmbot.merger import PUSD, USDC_DECIMALS, Merger, _calldata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("merge_all")

DATA_API = "https://data-api.polymarket.com/positions"


def _pusd(http: httpx.Client, rpc: str, wallet: str) -> float:
    data = _calldata("balanceOf(address)", ["address"], [wallet])
    r = http.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                             "params": [{"to": PUSD, "data": "0x" + data.hex()}, "latest"]})
    return int(r.json()["result"], 16) / USDC_DECIMALS


def main() -> None:
    load_dotenv()
    cfg = yaml.safe_load(open("config.yaml"))
    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ["POLYMARKET_FUNDER"]
    rpc = cfg["live"]["rpc_url"]
    creds = {"key": os.environ["POLYMARKET_BUILDER_API_KEY"],
             "secret": os.environ["POLYMARKET_BUILDER_SECRET"],
             "passphrase": os.environ["POLYMARKET_BUILDER_PASSPHRASE"]}

    merger = Merger(rpc, int(cfg["live"]["signature_type"]), key, funder,
                    relayer_url=cfg["live"].get("relayer_url"), builder_creds=creds)
    if merger.disabled:
        log.error("merger disabled: %s", merger.disabled); return

    http = httpx.Client(timeout=25.0, verify=certifi.where())
    r = http.get(DATA_API, params={"user": funder, "sizeThreshold": "0.01", "limit": 500})
    r.raise_for_status()
    by_cid: dict[str, dict] = defaultdict(dict)
    titles: dict[str, str] = {}
    for p in r.json():
        cid = p.get("conditionId")
        by_cid[cid][p.get("outcomeIndex")] = float(p.get("size", 0))
        titles[cid] = p.get("title", "")[:50]

    bal0 = _pusd(http, rpc, funder)
    log.info("pUSD before: $%.6f", bal0)
    merged_any = False
    for cid, outs in by_cid.items():
        if len(outs) < 2:
            continue
        pairs = float(int(min(outs.values())))
        if pairs < 1:
            continue
        log.info("merging %.0f pair(s) in %s (%s)", pairs, cid[:14], titles.get(cid, ""))
        ok = merger.merge(cid, False, pairs)
        log.info("  -> %s", "OK" if ok else "FAILED")
        merged_any = merged_any or ok

    bal1 = _pusd(http, rpc, funder)
    log.info("pUSD after:  $%.6f  (recovered $%+.6f)", bal1, bal1 - bal0)
    if not merged_any:
        log.info("nothing to merge (no offsetting whole pairs held)")


if __name__ == "__main__":
    main()
