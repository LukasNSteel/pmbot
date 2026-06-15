"""Probe which signature_type the V2 CLOB backend accepts for this wallet.

For each candidate signature_type, build a client, derive creds, sign and post
ONE deep (non-filling) GTD order on the funder, report the backend's response,
then cancel. The signature_type whose POST /orders succeeds is the one to put
in config.yaml. Always cleans up.
"""

from __future__ import annotations

import logging
import os
import time

import yaml
from dotenv import load_dotenv

from pmbot import gamma

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("probe")

HOST = "https://clob.polymarket.com"
CANDIDATES = [3, 2, 1]  # 3=POLY_1271 deposit wallet, 2=Gnosis Safe, 1=Magic proxy


def main() -> None:
    load_dotenv()
    from py_clob_client_v2 import (
        ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, Side,
    )
    from py_clob_client_v2.exceptions import PolyApiException

    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ["POLYMARKET_FUNDER"]
    cfg = yaml.safe_load(open("config.yaml"))

    log.info("scanning for one liquid market to probe with…")
    markets = gamma.scan(cfg)
    if not markets:
        log.error("no markets; cannot probe"); return
    m = markets[0]
    token = m.yes_token
    # Deep bid: 30% of a 0.5 reference, tick-rounded, clamped to >= tick.
    tick = m.tick
    price = max(tick, round(round(0.15 / tick) * tick, 6))
    size = float(int(max(m.min_size, 1)))
    log.info("probe market: %s (tick=%s neg_risk=%s)", m.question[:55], tick, m.neg_risk)
    log.info("probe order: BUY %.0f @ %.3f on token …%s", size, price, token[-6:])

    results: dict[int, str] = {}
    for st in CANDIDATES:
        log.info("──── signature_type=%d ────", st)
        try:
            client = ClobClient(HOST, chain_id=137, key=pk,
                                signature_type=st, funder=funder)
            client.set_api_creds(client.create_or_derive_api_key())
        except Exception as e:  # noqa: BLE001
            results[st] = f"client/creds error: {e}"
            log.error("  creds: %s", e)
            continue
        try:
            signed = client.create_order(
                OrderArgs(price=price, size=size, side=Side.BUY, token_id=token,
                          expiration=int(time.time()) + 150),
                PartialCreateOrderOptions(neg_risk=m.neg_risk),
            )
            resp = client.post_order(signed, OrderType.GTD)
            oid = resp.get("orderID") or resp.get("orderId") if isinstance(resp, dict) else None
            if oid:
                results[st] = f"ACCEPTED (orderID={oid[:18]}…)"
                log.info("  ACCEPTED — orderID=%s", oid)
            else:
                results[st] = f"posted, unexpected resp: {resp}"
                log.info("  posted, resp=%s", resp)
        except PolyApiException as e:
            results[st] = f"REJECTED: {e}"
            log.error("  REJECTED: %s", e)
        except Exception as e:  # noqa: BLE001
            results[st] = f"error: {e}"
            log.error("  error: %s", e)
        finally:
            try:
                client.cancel_all()
            except Exception as e:  # noqa: BLE001
                log.warning("  cancel_all: %s", e)

    log.info("════ SUMMARY ════")
    for st in CANDIDATES:
        log.info("  signature_type=%d → %s", st, results.get(st, "(skipped)"))


if __name__ == "__main__":
    main()
