"""One-off LIVE smoke test for deposit-wallet (signature_type 3) gasless merging.

It verifies the full production path used by Merger for type-3 wallets: builder
auth -> relayer WALLET nonce -> EIP-712 Batch signing -> DepositWalletFactory ->
wallet.execute -> pUSD collateral adapter -> confirmation polling.

Two modes, chosen automatically:
  * If the deposit wallet already holds a YES+NO pair (per the Data API), it
    merges ONE pair of that real inventory (recovers ~$1 pUSD).
  * Otherwise it runs a self-contained, economically-neutral round trip: split
    $1 pUSD into 1 YES + 1 NO, then merge it straight back to $1 pUSD — both
    calls in ONE atomic relayer batch (execute() reverts as a whole if either
    leg fails, so the wallet can never be left holding a half-done position).

Nothing here pays a spread or takes market risk; the only on-chain effect is a
zero-sum pUSD round trip. Gas is paid by the relayer.

Run:  .venv/bin/python -m scripts.merge_smoke
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

import certifi
import httpx
import yaml
from dotenv import load_dotenv

from pmbot import gamma
from pmbot.merger import (
    CTF_ADAPTER,
    PUSD,
    USDC_DECIMALS,
    Merger,
    _calldata,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("merge_smoke")

DATA_API = "https://data-api.polymarket.com/positions"
ZERO32 = b"\x00" * 32


def _pusd_balance(http: httpx.Client, rpc: str, wallet: str) -> float:
    data = _calldata("balanceOf(address)", ["address"], [wallet])
    resp = http.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                "params": [{"to": PUSD, "data": "0x" + data.hex()}, "latest"]})
    return int(resp.json()["result"], 16) / USDC_DECIMALS


def _find_mergeable_pair(http: httpx.Client, wallet: str):
    """Return (condition_id, neg_risk, pairs) for the largest real YES+NO pair,
    or None if the wallet holds no offsetting pair."""
    r = http.get(DATA_API, params={"user": wallet, "sizeThreshold": "0.01", "limit": 500})
    r.raise_for_status()
    by_cid: dict[str, dict] = defaultdict(dict)
    for p in r.json():
        by_cid[p.get("conditionId")][p.get("outcomeIndex", p.get("outcome"))] = p
    best = None
    for cid, outcomes in by_cid.items():
        if len(outcomes) < 2:
            continue
        sizes = [float(p.get("size", 0)) for p in outcomes.values()]
        pairs = min(sizes)
        neg = bool(next(iter(outcomes.values())).get("negativeRisk", False))
        if pairs >= 1 and (best is None or pairs > best[2]):
            best = (cid, neg, pairs)
    return best


def main() -> None:
    load_dotenv()
    cfg = yaml.safe_load(open("config.yaml"))
    if cfg["mode"] != "live":
        log.error("config mode is not 'live' — aborting"); return
    sig_type = int(cfg["live"]["signature_type"])
    if sig_type != 3:
        log.error("this smoke test targets signature_type 3 (deposit wallet); "
                  "config has %d — aborting", sig_type); return

    key = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ["POLYMARKET_FUNDER"]
    rpc = cfg["live"]["rpc_url"]
    creds = {
        "key": os.environ.get("POLYMARKET_BUILDER_API_KEY", ""),
        "secret": os.environ.get("POLYMARKET_BUILDER_SECRET", ""),
        "passphrase": os.environ.get("POLYMARKET_BUILDER_PASSPHRASE", ""),
    }
    if not all(creds.values()):
        log.error("missing POLYMARKET_BUILDER_API_KEY/_SECRET/_PASSPHRASE — aborting"); return

    log.info("== building Merger (relayer auth + deposit-wallet derivation) ==")
    merger = Merger(rpc, sig_type, key, funder,
                    relayer_url=cfg["live"].get("relayer_url"), builder_creds=creds)
    if merger.disabled:
        log.error("merger is disabled: %s", merger.disabled); return
    log.info("relayer ready; deposit wallet = %s", merger.wallet)

    http = httpx.Client(timeout=25.0, verify=certifi.where())

    # ---- Mode A: merge a real pair if one exists -------------------------
    pair = _find_mergeable_pair(http, funder)
    if pair is not None:
        cid, neg, pairs = pair
        log.info("== REAL MERGE: found %.2f mergeable pairs in %s (neg_risk=%s) ==",
                 pairs, cid[:14], neg)
        bal0 = _pusd_balance(http, rpc, funder)
        log.info("pUSD before: $%.6f", bal0)
        ok = merger.merge(cid, neg, 1)  # merge exactly one pair
        bal1 = _pusd_balance(http, rpc, funder)
        log.info("pUSD after:  $%.6f  (delta $%+.6f)", bal1, bal1 - bal0)
        log.info("RESULT: %s", "PASS — real merge confirmed on-chain" if ok else "FAIL")
        return

    # ---- Mode B: self-contained split -> merge round trip ----------------
    log.info("== no resting YES+NO pair; running split->merge round trip ($1) ==")
    log.info("scanning for a standard (non-neg-risk) market to split into…")
    markets = gamma.scan(cfg)
    target = next((m for m in markets
                   if not m.neg_risk
                   and len(bytes.fromhex(m.condition_id.removeprefix("0x"))) == 32),
                  None)
    if target is None:
        log.error("no suitable standard market found to split into — aborting"); return
    cid = bytes.fromhex(target.condition_id.removeprefix("0x"))
    log.info("market: %s", target.question[:70])
    log.info("  condition_id=%s", target.condition_id)

    amount = 1 * USDC_DECIMALS  # $1 -> 1 YES + 1 NO, merged straight back
    split_call = _calldata(
        "splitPosition(address,bytes32,bytes32,uint256[],uint256)",
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [PUSD, ZERO32, cid, [1, 2], amount])
    merge_call = _calldata(
        "mergePositions(address,bytes32,bytes32,uint256[],uint256)",
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        [PUSD, ZERO32, cid, [1, 2], amount])
    calls = [(CTF_ADAPTER, split_call), (CTF_ADAPTER, merge_call)]

    bal0 = _pusd_balance(http, rpc, funder)
    log.info("pUSD before: $%.6f", bal0)
    log.info("submitting atomic split+merge batch via relayer…")
    t0 = time.time()
    try:
        ok = merger._execute_via_relayer(calls)
    except Exception as e:  # noqa: BLE001
        log.error("RESULT: FAIL — relayer batch errored: %s", e); return
    bal1 = _pusd_balance(http, rpc, funder)
    log.info("pUSD after:  $%.6f  (delta $%+.6f, %.1fs)", bal1, bal1 - bal0, time.time() - t0)

    neutral = abs(bal1 - bal0) < 1e-3
    if ok and neutral:
        log.info("RESULT: PASS — gasless deposit-wallet batch confirmed on-chain; "
                 "split+merge round-tripped pUSD with no net change")
    elif ok:
        log.warning("RESULT: CONFIRMED but pUSD delta $%+.6f is unexpectedly large "
                    "— inspect manually", bal1 - bal0)
    else:
        log.error("RESULT: FAIL — batch did not confirm")


if __name__ == "__main__":
    main()
