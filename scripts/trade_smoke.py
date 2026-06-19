"""One-off LIVE filled-trade smoke test.

Places REAL marketable buys on BOTH sides of one market to acquire a small
YES+NO complete set (a genuine CLOB fill on each side), then merges the pair
back to pUSD via the gasless relayer. End state is riskless: a complete set is
worth exactly $1/pair at resolution, and the merge recovers it immediately as
spendable pUSD. Net cost is just the spread over $1.00 plus taker fees.

This exercises the full live trading + reinvestment loop with the smallest
order the exchange will accept.

Run:  .venv/bin/python -m scripts.trade_smoke
"""

from __future__ import annotations

import asyncio
import logging
import os

import certifi
import httpx
import yaml
from dotenv import load_dotenv

from pmbot import gamma
from pmbot.books import BookTracker
from pmbot.brokers import LiveBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("pmbot.books").setLevel(logging.WARNING)
log = logging.getLogger("trade_smoke")

DATA_API = "https://data-api.polymarket.com/positions"
# Smallest sizes (shares) to attempt per leg, in order. Polymarket enforces a
# per-order minimum (~$1 notional / a few shares); escalate until accepted.
SIZE_LADDER = [5.0, 10.0]


def _position_pairs(funder: str, cid: str) -> float:
    with httpx.Client(timeout=15.0, verify=certifi.where()) as http:
        r = http.get(DATA_API, params={"user": funder, "sizeThreshold": "0.01",
                                        "limit": 500})
        r.raise_for_status()
        yes = no = 0.0
        for p in r.json():
            if str(p.get("conditionId")) != cid:
                continue
            idx = p.get("outcomeIndex")
            if idx in (0, "0"):
                yes = float(p.get("size", 0))
            elif idx in (1, "1"):
                no = float(p.get("size", 0))
        return min(yes, no)


async def main() -> None:
    load_dotenv()
    cfg = yaml.safe_load(open("config.yaml"))
    if cfg["mode"] != "live":
        log.error("config mode is not 'live' — aborting"); return

    log.info("== scanning for a liquid, tight, standard market ==")
    markets = await asyncio.to_thread(gamma.scan, cfg)
    target = None
    for m in markets:
        if m.neg_risk:
            continue
        if len(bytes.fromhex(m.condition_id.removeprefix("0x"))) != 32:
            continue
        target = m
        break
    if target is None:
        log.error("no suitable standard market found — aborting"); return
    m = target
    log.info("market: %s", m.question[:70])
    log.info("  cid=%s tick=%s", m.condition_id, m.tick)

    tracker = BookTracker([m.yes_token, m.no_token])
    await tracker.start()
    await asyncio.sleep(3.0)
    yb, nb = tracker.books[m.yes_token], tracker.books[m.no_token]
    log.info("  YES bid=%s ask=%s | NO bid=%s ask=%s",
             yb.best_bid, yb.best_ask, nb.best_bid, nb.best_ask)
    if yb.best_ask is None or nb.best_ask is None:
        log.error("a side has no ask; cannot buy a set — aborting"); await tracker.stop(); return

    broker = LiveBroker(cfg, tracker)
    await asyncio.to_thread(broker.refresh_state)
    log.info("== broker ready: collateral=$%.4f equity=$%.4f ==",
             broker._collateral, broker.equity())

    pair_px = yb.best_ask + nb.best_ask
    log.info("set price ~$%.3f/pair (YES ask %.3f + NO ask %.3f); merge recovers $1.00/pair",
             pair_px, yb.best_ask, nb.best_ask)

    # --- buy both legs marketable at the smallest accepted size ---
    size = None
    for candidate in SIZE_LADDER:
        log.info("-- attempting YES buy: %.0f sh @ <=%.3f --", candidate, yb.best_ask)
        filled_yes = await asyncio.to_thread(
            broker.taker_buy, m, m.yes_token, candidate, round(yb.best_ask + 0.01, 2))
        if filled_yes > 0:
            size = candidate
            log.info("   YES filled %.2f sh", filled_yes)
            break
        log.warning("   YES buy did not fill at size %.0f; escalating", candidate)
    if size is None:
        log.error("could not get a YES fill at any ladder size — aborting"); await tracker.stop(); return

    log.info("-- buying NO leg: %.0f sh @ <=%.3f --", size, nb.best_ask)
    filled_no = await asyncio.to_thread(
        broker.taker_buy, m, m.no_token, size, round(nb.best_ask + 0.01, 2))
    log.info("   NO filled %.2f sh", filled_no)

    await asyncio.sleep(2.0)
    await asyncio.to_thread(broker.refresh_state)
    pairs = _position_pairs(os.environ["POLYMARKET_FUNDER"], m.condition_id)
    log.info("== acquired %.2f YES+NO pairs (riskless complete set) ==", pairs)

    # --- merge the set back to spendable pUSD (reinvestment loop) ---
    if pairs >= 1 and broker.merger and not broker.merger.disabled:
        whole = float(int(pairs))
        log.info("== merging %.0f pair(s) back to pUSD via relayer ==", whole)
        ok = await asyncio.to_thread(broker.merger.merge, m.condition_id, m.neg_risk, whole)
        log.info("merge result: %s", "PASS — recovered $%.2f pUSD" % whole if ok else "FAIL")
    else:
        log.warning("not merging (pairs=%.2f, merger=%s); any unpaired leg stays as a "
                    "position the running bot will manage", pairs, bool(broker.merger))

    await asyncio.to_thread(broker.refresh_state)
    log.info("== final: collateral=$%.4f equity=$%.4f ==",
             broker._collateral, broker.equity())
    await tracker.stop()


if __name__ == "__main__":
    asyncio.run(main())
