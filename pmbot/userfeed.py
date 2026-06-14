"""Authenticated user-channel WebSocket (live mode): real-time fill events.

The CLOB pushes a `trade` message the moment one of our orders matches, so
fills drive the breakers/fading/markouts immediately instead of waiting for
the ~12s position poll (which stays on as reconciliation). While this feed
is connected the broker's poll-based fill detection is disabled; if the
feed drops, polling takes over until the feed reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import websockets

from .books import SSL_CONTEXT

log = logging.getLogger("pmbot.userfeed")

WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class UserFeed:
    def __init__(self, broker):
        self.broker = broker
        creds = broker.client.creds
        self._auth = {
            "apiKey": creds.api_key,
            "secret": creds.api_secret,
            "passphrase": creds.api_passphrase,
        }
        self._stop = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        self.broker.ws_fills_active = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while not self._stop:
            try:
                async with websockets.connect(WS_USER_URL, ping_interval=None,
                                              ssl=SSL_CONTEXT) as ws:
                    await ws.send(json.dumps({"type": "user", "auth": self._auth}))
                    log.info("user feed connected — real-time fill detection active")
                    self.broker.ws_fills_active = True
                    ping = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            if raw == "PONG":
                                continue
                            self._handle(raw)
                    finally:
                        ping.cancel()
            except Exception as e:  # noqa: BLE001 — reconnect on any socket failure
                if not self._stop:
                    self.broker.ws_fills_active = False
                    log.warning("user feed dropped (%s); reconnecting in 3s "
                                "(poll-based fill detection until then)", e)
                    await asyncio.sleep(3)

    @staticmethod
    async def _ping(ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        events = msg if isinstance(msg, list) else [msg]
        for ev in events:
            if not isinstance(ev, dict) or ev.get("event_type") != "trade":
                continue
            # MATCHED fires once at match time; the later MINED/CONFIRMED
            # updates for the same trade are skipped to avoid double counts.
            if str(ev.get("status") or "") != "MATCHED":
                continue
            try:
                self._handle_trade(ev)
            except Exception as e:  # noqa: BLE001 — never let one bad event kill the feed
                log.warning("bad user-feed trade event: %s", e)

    def _handle_trade(self, ev: dict) -> None:
        taker_side = str(ev.get("side") or "").upper()
        taker_outcome = ev.get("outcome")
        ours = self.broker.address.lower()
        we_are_maker = False
        for mo in ev.get("maker_orders") or []:
            if str(mo.get("maker_address") or "").lower() != ours:
                continue
            we_are_maker = True
            token = str(mo.get("asset_id") or "")
            price = float(mo.get("price") or 0)
            size = float(mo.get("matched_amount") or 0)
            if mo.get("outcome") == taker_outcome:
                # Same-token match: we took the other side of the taker.
                side = "SELL" if taker_side == "BUY" else "BUY"
            else:
                # Complementary match (mint/burn): both parties trade in the
                # same direction, each on their own token.
                side = taker_side
            if token and size > 0:
                self.broker.record_user_fill(token, side, price, size, taker=False)
        if not we_are_maker:
            # None of the maker orders are ours, so this event is about our
            # own taker order (e.g. a forced hedge crossing the spread).
            token = str(ev.get("asset_id") or "")
            price = float(ev.get("price") or 0)
            size = float(ev.get("size") or 0)
            if token and size > 0:
                self.broker.record_user_fill(token, taker_side, price, size, taker=True)
