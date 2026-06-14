"""Real-time orderbook tracker: CLOB WebSocket with REST polling fallback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import time

import certifi
import httpx
import websockets

log = logging.getLogger("pmbot.books")

# Some Python installs (notably python.org macOS builds) have no system CA
# bundle wired into ssl; use certifi's explicitly.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_URL = "https://clob.polymarket.com"
REST_POLL_SECONDS = 10.0  # only used when the socket is unhealthy
WS_STALE_SECONDS = 30.0


class Book:
    """One side-pair orderbook for a single token."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_trade_price: float | None = None
        self.last_trade_ts: float = 0.0
        self.updated_ts: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        return self.last_trade_price

    def snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids = {float(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {float(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}
        self.updated_ts = time.time()

    def apply_change(self, side: str, price: float, size: float) -> None:
        levels = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.updated_ts = time.time()

    def depth_within(self, center: float, band: float, side: str) -> list[tuple[float, float]]:
        """(price, size) levels within `band` of `center` on one side."""
        levels = self.bids if side == "bid" else self.asks
        return [(p, s) for p, s in levels.items() if abs(p - center) <= band]


class BookTracker:
    """Maintains Books for a set of token ids; notifies listeners on events."""

    def __init__(self, token_ids: list[str], carry: dict[str, Book] | None = None):
        carry = carry or {}
        self.books: dict[str, Book] = {
            t: carry[t] if t in carry else Book(t) for t in token_ids
        }
        self.last_msg_ts: float = time.time()  # any WS traffic = feed is alive
        self._trade_listeners: list = []  # async callbacks (token_id, price, side, size)
        self._stop = False
        self._tasks: list[asyncio.Task] = []

    def on_trade(self, callback) -> None:
        self._trade_listeners.append(callback)

    async def start(self) -> None:
        await self._rest_refresh_all()  # prime books before quoting
        self._tasks = [
            asyncio.create_task(self._ws_loop()),
            asyncio.create_task(self._rest_fallback_loop()),
        ]

    async def stop(self) -> None:
        self._stop = True
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    # ------------------------------------------------------------- websocket

    async def _ws_loop(self) -> None:
        while not self._stop:
            try:
                async with websockets.connect(WS_URL, ping_interval=None, ssl=SSL_CONTEXT) as ws:
                    await ws.send(json.dumps({"type": "market", "assets_ids": list(self.books)}))
                    log.info("WebSocket subscribed to %d tokens", len(self.books))
                    ping = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self.last_msg_ts = time.time()
                            if raw == "PONG":
                                continue
                            await self._handle(raw)
                    finally:
                        ping.cancel()
            except Exception as e:  # noqa: BLE001 — reconnect on any socket failure
                if not self._stop:
                    log.warning("WebSocket dropped (%s); reconnecting in 3s", e)
                    await asyncio.sleep(3)

    @staticmethod
    async def _ping(ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    async def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        events = msg if isinstance(msg, list) else [msg]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("event_type") or ev.get("type")
            token = str(ev.get("asset_id") or "")
            book = self.books.get(token)
            if book is None:
                continue
            if etype == "book":
                bids = ev.get("bids") or ev.get("buys") or []
                asks = ev.get("asks") or ev.get("sells") or []
                book.snapshot(bids, asks)
            elif etype == "price_change":
                for ch in ev.get("changes") or [ev]:
                    try:
                        book.apply_change(
                            str(ch.get("side") or ""),
                            float(ch.get("price")),
                            float(ch.get("size")),
                        )
                    except (TypeError, ValueError):
                        continue
            elif etype == "last_trade_price":
                try:
                    price = float(ev.get("price"))
                except (TypeError, ValueError):
                    continue
                side = str(ev.get("side") or "")
                try:
                    size = float(ev.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0.0
                book.last_trade_price = price
                book.last_trade_ts = time.time()
                for cb in self._trade_listeners:
                    await cb(token, price, side, size)

    # ------------------------------------------------------------- REST

    async def _rest_fallback_loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(REST_POLL_SECONDS)
            stale = [
                t for t, b in self.books.items()
                if time.time() - b.updated_ts > WS_STALE_SECONDS
            ]
            if stale:
                log.debug("REST refresh for %d stale books", len(stale))
                await self._rest_refresh(stale)

    async def _rest_refresh_all(self) -> None:
        await self._rest_refresh(list(self.books))

    async def _rest_refresh(self, token_ids: list[str]) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for token in token_ids:
                try:
                    resp = await client.get(f"{CLOB_URL}/book", params={"token_id": token})
                    resp.raise_for_status()
                    data = resp.json()
                    self.books[token].snapshot(data.get("bids") or [], data.get("asks") or [])
                except Exception as e:  # noqa: BLE001
                    log.debug("REST book fetch failed for %s…: %s", token[:12], e)
