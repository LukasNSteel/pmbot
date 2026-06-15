"""One-off LIVE smoke test for the CLOB V2 migration.

Places a SINGLE deep, far-from-mid resting BUY (designed never to fill),
verifies it is parsed/tracked and visible on the exchange, then cancels it.
Always cleans up. Confirms: V2 auth/creds, order signing + version resolution,
tick-size validation, batch post_orders response parsing (B2 safety net),
reconcile, and cancel.

Run:  .venv/bin/python -m scripts.live_smoke
"""

from __future__ import annotations

import asyncio
import logging

import yaml
from dotenv import load_dotenv

from pmbot import gamma
from pmbot.books import BookTracker
from pmbot.brokers import LiveBroker
from pmbot.strategy import Quote, _round_tick

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("pmbot.books").setLevel(logging.WARNING)
log = logging.getLogger("smoke")


async def main() -> None:
    load_dotenv()
    cfg = yaml.safe_load(open("config.yaml"))
    if cfg["mode"] != "live":
        log.error("config mode is not 'live' — aborting"); return

    log.info("== scanning for a liquid reward market ==")
    markets = await asyncio.to_thread(gamma.scan, cfg)
    if not markets:
        log.error("scan returned no markets; cannot run smoke test"); return
    m = markets[0]
    log.info("market: %s", m.question[:70])
    log.info("  tick=%s neg_risk=%s min_size=%s end=%s", m.tick, m.neg_risk,
             m.min_size, m.end_date)

    tracker = BookTracker([m.yes_token, m.no_token])
    await tracker.start()
    await asyncio.sleep(3.0)  # let the WS/REST prime the book
    yb = tracker.books[m.yes_token]
    log.info("  YES book: bid=%s ask=%s mid=%s", yb.best_bid, yb.best_ask, yb.mid)

    log.info("== constructing LiveBroker (auth + creds derivation) ==")
    broker = LiveBroker(cfg, tracker)
    await asyncio.to_thread(broker.refresh_state)
    log.info("  address=%s", broker.address)
    log.info("  collateral=$%.4f  equity=$%.4f", broker._collateral, broker.equity())

    # Deep, valid bid that will not fill within the test window.
    ref = yb.best_bid or yb.mid or 0.5
    test_price = _round_tick(max(m.tick, ref - 0.05), m.tick)
    size = float(int(max(m.min_size, 1)))
    cost = test_price * size
    log.info("== test order: BUY %s YES @ %.3f  (locks ~$%.2f collateral) ==",
             size, test_price, cost)

    # ---- TIER 1: sign locally, no post (zero risk) ----
    from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions, Side
    try:
        broker.client.create_order(
            OrderArgs(price=test_price, size=size, side=Side.BUY,
                      token_id=m.yes_token, expiration=broker._gtd_expiration()),
            PartialCreateOrderOptions(neg_risk=m.neg_risk),
        )
        log.info("TIER1 PASS: order signed locally; version=%s tick resolved OK",
                 broker.client.get_version())
    except Exception as e:  # noqa: BLE001
        log.error("TIER1 FAIL (signing/tick/version): %s", e)
        await tracker.stop(); return

    # ---- TIER 2: place via batch path, verify tracking, reconcile, cancel ----
    try:
        await asyncio.to_thread(broker.set_quotes, m, [Quote(m.yes_token, test_price, size)])
        tracked = broker.open_quotes(m)
        log.info("TIER2 after set_quotes: %d order(s) tracked locally: %s",
                 len(tracked), [(round(q.price, 3), q.size) for q in tracked])

        await asyncio.to_thread(broker.reconcile_orders)
        remote = broker.open_quotes(m)
        log.info("TIER2 after reconcile (exchange truth): %d order(s): %s",
                 len(remote), [(round(q.price, 3), q.size) for q in remote])
        if remote:
            log.info("TIER2 PASS: order placed and visible on exchange "
                     "(batch post parse + tracking OK)")
        else:
            log.warning("TIER2 INCONCLUSIVE: nothing resting after reconcile — "
                        "check for an insufficient-balance or price-band reject above")
    finally:
        log.info("== cleanup: cancelling all orders ==")
        await asyncio.to_thread(broker.cancel_all)
        await asyncio.to_thread(broker.reconcile_orders)
        leftover = broker.open_quotes(m)
        if leftover:
            log.error("CLEANUP WARNING: %d order(s) still resting: %s — cancel manually!",
                      len(leftover), [(round(q.price, 3), q.size) for q in leftover])
        else:
            log.info("cleanup PASS: no resting orders remain")
        await tracker.stop()


if __name__ == "__main__":
    asyncio.run(main())
