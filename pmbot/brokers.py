"""Brokers: PaperBroker (fill simulation, positions, PnL) and LiveBroker
(py-clob-client-v2 wrapper). Both expose the same interface."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .books import BookTracker
from .gamma import Market
from .strategy import Quote

log = logging.getLogger("pmbot.broker")

ORDER_RECONCILE_SECONDS = 30.0
# Polymarket GTD orders carry a 1-minute security threshold: an order with
# expiration=T is effectively dead at ~T-60s, so quote for ttl seconds we
# must sign expiration=now+ttl+60 and refresh well before T-60.
GTD_SECURITY_THRESHOLD_SECS = 60
GTD_REFRESH_MARGIN_SECS = GTD_SECURITY_THRESHOLD_SECS + 30
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_DECIMALS = 1_000_000


@dataclass
class Position:
    yes_shares: float = 0.0
    no_shares: float = 0.0
    merged_usd: float = 0.0
    fills: int = 0

    def merge(self) -> float:
        pairs = min(self.yes_shares, self.no_shares)
        if pairs > 0:
            self.yes_shares -= pairs
            self.no_shares -= pairs
            self.merged_usd += pairs
        return pairs


@dataclass
class PaperState:
    cash: float
    start_equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    est_rewards: float = 0.0
    fills_log: list[dict] = field(default_factory=list)


@dataclass
class RestingOrder:
    order_id: str
    quote: Quote
    placed_ts: float
    expiration: int = 0


@dataclass
class PaperQuoteState:
    quote: Quote
    queue_ahead: float = 0.0
    active_at: float = 0.0  # resting on the book only once now >= active_at


def _parse_fill_amount(resp: dict, requested: float) -> float:
    for key in ("takingAmount", "taking_amount", "size_matched", "sizeMatched"):
        val = resp.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    if resp.get("success") is False or resp.get("error"):
        return 0.0
    status = str(resp.get("status") or "").upper()
    if status in ("MATCHED", "FILLED", "LIVE"):
        return requested
    return 0.0


def _parse_erc20_balance(result: str | None) -> float:
    if not result:
        raise ValueError("empty eth_call result")
    return int(result, 16) / USDC_DECIMALS


class PaperBroker:
    """Simulates fills against the live book with a queue-position model,
    simulated order latency (placement and cancellation each take
    `latency_secs` — stale quotes stay fillable until the cancel "lands"),
    depth-aware taker fills, and maker fees."""

    def __init__(self, capital: float, tracker: BookTracker, data_dir: str = "data",
                 latency_secs: float = 0.0):
        self.state = PaperState(cash=capital, start_equity=capital)
        self.tracker = tracker
        self.latency = latency_secs
        self._quotes: dict[str, list[PaperQuoteState]] = {}
        self._exits: dict[str, PaperQuoteState] = {}
        # Quotes whose cancel is still in flight: list of (quote, fillable_until).
        self._dying: dict[str, list[tuple[Quote, float]]] = {}
        self._markets: dict[str, Market] = {}
        self._token_to_market: dict[str, Market] = {}
        self._last_mids: dict[str, float] = {}
        self._data_path = Path(data_dir) / "paper_state.json"
        self._data_path.parent.mkdir(exist_ok=True)
        self.metrics = None
        tracker.on_trade(self._on_trade)

    def _fee_usd(self, market: Market, price: float, size: float) -> float:
        """Polymarket fee formula: rate × min(p, 1−p) × shares."""
        if market.fee_bps <= 0:
            return 0.0
        return market.fee_bps / 10000.0 * min(price, 1.0 - price) * size

    def _start_dying(self, cid: str, st: PaperQuoteState, now: float) -> None:
        """A cancelled/replaced quote rests on the book until the cancel lands."""
        if now >= st.active_at and self.latency > 0:
            self._dying.setdefault(cid, []).append((st.quote, now + self.latency))

    def set_quotes(self, market: Market, quotes: list[Quote]) -> None:
        self._markets[market.condition_id] = market
        self._token_to_market[market.yes_token] = market
        self._token_to_market[market.no_token] = market
        now = time.time()
        cur = {s.quote.token_id: s for s in self._quotes.get(market.condition_id, [])}
        new_states = []
        for q in quotes:
            prev = cur.pop(q.token_id, None)
            if prev is not None and prev.quote.key() == q.key():
                new_states.append(prev)
                continue
            if prev is not None:
                self._start_dying(market.condition_id, prev, now)
            book = self.tracker.books.get(q.token_id)
            ahead = book.bids.get(q.price, 0.0) if book else 0.0
            new_states.append(PaperQuoteState(
                quote=q, queue_ahead=ahead, active_at=now + self.latency))
        for prev in cur.values():  # no longer desired
            self._start_dying(market.condition_id, prev, now)
        self._quotes[market.condition_id] = new_states

    def cancel_all(self) -> None:
        self.cancel_quotes()
        self._exits.clear()

    def cancel_quotes(self) -> None:
        now = time.time()
        for cid, states in self._quotes.items():
            for st in states:
                self._start_dying(cid, st, now)
        self._quotes.clear()

    def open_quotes(self, market: Market) -> list[Quote]:
        return [s.quote for s in self._quotes.get(market.condition_id, [])]

    def set_exit(self, market: Market, quote: Quote | None) -> None:
        cid = market.condition_id
        if quote is None:
            self._exits.pop(cid, None)
            return
        cur = self._exits.get(cid)
        if cur is not None and cur.quote.key() == quote.key():
            return
        self._markets[cid] = market
        self._token_to_market[market.yes_token] = market
        self._token_to_market[market.no_token] = market
        book = self.tracker.books.get(quote.token_id)
        ahead = book.asks.get(quote.price, 0.0) if book else 0.0
        self._exits[cid] = PaperQuoteState(
            quote=quote, queue_ahead=ahead, active_at=time.time() + self.latency)

    def exit_quote(self, market: Market) -> Quote | None:
        cur = self._exits.get(market.condition_id)
        return cur.quote if cur else None

    @property
    def fills_log(self) -> list[dict]:
        return self.state.fills_log

    async def _on_trade(self, token_id: str, trade_price: float,
                        side: str = "", size: float = 0.0) -> None:
        market = self._token_to_market.get(token_id)
        if market is None:
            return
        now = time.time()
        cid = market.condition_id

        # Stale quotes whose cancel hasn't landed yet get picked off by
        # through-prints — the dominant live cost paper used to miss.
        keep_dying = []
        for q, until in self._dying.get(cid, []):
            if now >= until:
                continue
            if q.token_id == token_id and trade_price < q.price - 1e-9:
                fill_sz = min(q.size, size) if size > 0 else q.size
                self._fill(market, Quote(q.token_id, q.price, fill_sz), fill_sz)
            else:
                keep_dying.append((q, until))
        self._dying[cid] = keep_dying

        states = self._quotes.get(cid, [])
        remaining = []
        for st in states:
            q = st.quote
            if q.token_id != token_id:
                remaining.append(st)
                continue
            active = now >= st.active_at
            if trade_price < q.price - 1e-9:
                if not active:
                    # Level swept while our order was in flight: we missed the
                    # trade but will be alone at the level once we land.
                    st.queue_ahead = 0.0
                    remaining.append(st)
                    continue
                # Price priority guarantees the fill, but only for the taker's size.
                fill_sz = min(q.size, size) if size > 0 else q.size
                self._fill(market, Quote(q.token_id, q.price, fill_sz), fill_sz)
                if fill_sz < q.size - 1e-9:
                    st.quote = Quote(q.token_id, q.price, q.size - fill_sz)
                    st.queue_ahead = 0.0
                    remaining.append(st)
            elif abs(trade_price - q.price) < 1e-9 and size > 0:
                consume = min(size, st.queue_ahead)
                st.queue_ahead -= consume
                leftover = size - consume
                if active and leftover > 0 and st.queue_ahead <= 1e-9:
                    fill_sz = min(leftover, q.size)
                    self._fill(market, Quote(q.token_id, q.price, fill_sz), fill_sz)
                    if fill_sz < q.size - 1e-9:
                        st.quote = Quote(q.token_id, q.price, q.size - fill_sz)
                        remaining.append(st)
                else:
                    remaining.append(st)
            else:
                remaining.append(st)
        self._quotes[cid] = remaining

        ex = self._exits.get(cid)
        if ex is not None and ex.quote.token_id == token_id:
            active = now >= ex.active_at
            if trade_price > ex.quote.price + 1e-9:
                if active:
                    self._fill_exit(market, ex.quote, ex.quote.size)
                    self._exits.pop(cid, None)
            elif abs(trade_price - ex.quote.price) < 1e-9 and size > 0:
                consume = min(size, ex.queue_ahead)
                ex.queue_ahead -= consume
                leftover = size - consume
                if active and leftover > 0 and ex.queue_ahead <= 1e-9:
                    fill_sz = min(leftover, ex.quote.size)
                    self._fill_exit(market, ex.quote, fill_sz)
                    if fill_sz >= ex.quote.size - 1e-9:
                        self._exits.pop(cid, None)
                    else:
                        ex.quote = Quote(token_id, ex.quote.price,
                                         ex.quote.size - fill_sz)

    def check_crossed_books(self) -> None:
        now = time.time()
        for cid, dies in list(self._dying.items()):
            keep = []
            for q, until in dies:
                if now >= until:
                    continue
                book = self.tracker.books.get(q.token_id)
                ask = book.best_ask if book else None
                if ask is not None and ask <= q.price:
                    self._fill(self._markets[cid], q, q.size)
                else:
                    keep.append((q, until))
            self._dying[cid] = keep
        for cid, states in list(self._quotes.items()):
            market = self._markets[cid]
            remaining = []
            for st in states:
                q = st.quote
                book = self.tracker.books.get(q.token_id)
                ask = book.best_ask if book else None
                if ask is not None and ask <= q.price and now >= st.active_at:
                    self._fill(market, q, q.size)
                else:
                    if book is not None:
                        # People ahead of us cancelling shortens our queue.
                        st.queue_ahead = min(st.queue_ahead,
                                             book.bids.get(q.price, 0.0))
                    remaining.append(st)
            self._quotes[cid] = remaining
        for cid, ex in list(self._exits.items()):
            book = self.tracker.books.get(ex.quote.token_id)
            bid = book.best_bid if book else None
            if bid is not None and bid >= ex.quote.price and now >= ex.active_at:
                self._fill_exit(self._markets[cid], ex.quote, ex.quote.size)
                self._exits.pop(cid, None)

    def _fill(self, market: Market, q: Quote, size: float) -> None:
        pos = self.state.positions.setdefault(market.condition_id, Position())
        fee = self._fee_usd(market, q.price, size)
        cost = q.price * size + fee
        self.state.cash -= cost
        if q.token_id == market.yes_token:
            pos.yes_shares += size
            side = "YES"
        else:
            pos.no_shares += size
            side = "NO"
        pos.fills += 1
        merged = pos.merge()
        if merged:
            self.state.cash += merged
        entry = {
            "ts": time.time(), "cid": market.condition_id,
            "market": market.question[:50], "side": side,
            "token": q.token_id, "price": q.price, "size": size, "merged": merged,
        }
        if fee > 0:
            entry["fee"] = fee
        self.state.fills_log.append(entry)
        if self.metrics:
            self.metrics.record_fill(entry)
            if merged:
                self.metrics.record_merge(market.condition_id, merged)
        log.info("FILL %s %s %.0f @ %.3f (merged %.0f pairs%s)",
                 market.question[:40], side, size, q.price, merged,
                 f", fee ${fee:.2f}" if fee > 0 else "")
        self._persist()

    def _fill_exit(self, market: Market, q: Quote, fill_sz: float) -> None:
        pos = self.state.positions.setdefault(market.condition_id, Position())
        if q.token_id == market.yes_token:
            size = min(fill_sz, pos.yes_shares)
            pos.yes_shares -= size
            side = "YES"
        else:
            size = min(fill_sz, pos.no_shares)
            pos.no_shares -= size
            side = "NO"
        if size <= 0:
            return
        fee = self._fee_usd(market, q.price, size)
        self.state.cash += q.price * size - fee
        pos.fills += 1
        entry = {
            "ts": time.time(), "cid": market.condition_id,
            "market": market.question[:50], "side": side,
            "token": q.token_id, "price": q.price, "size": size, "exit": True,
        }
        if fee > 0:
            entry["fee"] = fee
        self.state.fills_log.append(entry)
        if self.metrics:
            self.metrics.record_fill(entry)
        log.info("EXIT FILL %s sold %.0f %s @ %.3f",
                 market.question[:40], size, side, q.price)
        self._persist()

    def taker_buy(self, market: Market, token_id: str, size: float, max_price: float) -> float:
        book = self.tracker.books.get(token_id)
        if book is None or not book.asks:
            return 0.0
        # Walk displayed depth up to max_price — a real FAK fills partially.
        remaining = size
        cost = 0.0
        filled = 0.0
        for price in sorted(book.asks):
            if price > max_price + 1e-9 or remaining <= 1e-9:
                break
            take = min(remaining, book.asks[price])
            cost += take * price
            filled += take
            remaining -= take
        if filled <= 0:
            return 0.0
        avg = cost / filled
        pos = self.state.positions.setdefault(market.condition_id, Position())
        fee = self._fee_usd(market, avg, filled)
        self.state.cash -= cost + fee
        if token_id == market.yes_token:
            pos.yes_shares += filled
            side = "YES"
        else:
            pos.no_shares += filled
            side = "NO"
        pos.fills += 1
        merged = pos.merge()
        if merged:
            self.state.cash += merged
        entry = {
            "ts": time.time(), "cid": market.condition_id,
            "market": market.question[:50], "side": side,
            "token": token_id, "price": avg, "size": filled, "merged": merged,
            "taker": True,
        }
        if fee > 0:
            entry["fee"] = fee
        self.state.fills_log.append(entry)
        if self.metrics:
            self.metrics.record_fill(entry)
            self.metrics.record_hedge(market.condition_id, avg, filled)
            if merged:
                self.metrics.record_merge(market.condition_id, merged)
        log.info("TAKER %s %s %.0f @ %.3f (merged %.0f pairs%s)",
                 market.question[:40], side, filled, avg, merged,
                 f", fee ${fee:.2f}" if fee > 0 else "")
        self._persist()
        return filled

    def position_tokens(self) -> list[str]:
        tokens = []
        for cid, pos in self.state.positions.items():
            if pos.yes_shares > 0 or pos.no_shares > 0:
                market = self._markets.get(cid)
                if market:
                    tokens.extend((market.yes_token, market.no_token))
        return tokens

    def equity(self) -> float:
        total = self.state.cash + self.state.est_rewards
        for cid, pos in self.state.positions.items():
            market = self._markets.get(cid)
            if market is None:
                continue
            yes_mid = self._mark(market.yes_token)
            no_mid = self._mark(market.no_token)
            if no_mid is None:
                no_mid = 1 - yes_mid if yes_mid is not None else 0.5
            if yes_mid is None:
                yes_mid = 1 - no_mid
            total += pos.yes_shares * yes_mid + pos.no_shares * no_mid
        return total

    def _mark(self, token_id: str) -> float | None:
        book = self.tracker.books.get(token_id)
        mid = book.mid if book else None
        if mid is not None:
            self._last_mids[token_id] = mid
            return mid
        return self._last_mids.get(token_id)

    def net_yes_exposure_usd(self, market: Market) -> float:
        pos = self.state.positions.get(market.condition_id)
        if pos is None:
            return 0.0
        mid = self._mark(market.yes_token)
        if mid is None:
            mid = 0.5
        return pos.yes_shares * mid - pos.no_shares * (1 - mid)

    def unpaired_shares(self, market: Market) -> float:
        pos = self.state.positions.get(market.condition_id)
        if pos is None:
            return 0.0
        return pos.yes_shares - pos.no_shares

    def held_markets(self) -> list[Market]:
        return [
            self._markets[cid]
            for cid, pos in self.state.positions.items()
            if cid in self._markets and (pos.yes_shares > 0 or pos.no_shares > 0)
        ]

    def total_inventory_usd(self) -> float:
        return sum(
            abs(self.net_yes_exposure_usd(m)) for m in self._markets.values()
        )

    def accrue_rewards(self, usd: float) -> None:
        self.state.est_rewards += usd
        if self.metrics:
            self.metrics.record_est_reward(usd)

    def _persist(self) -> None:
        try:
            self._data_path.write_text(json.dumps({
                "cash": self.state.cash,
                "est_rewards": self.state.est_rewards,
                "fills": self.state.fills_log[-200:],
            }, indent=2))
        except OSError as e:
            log.warning("could not persist state: %s", e)


class LiveBroker:
    """Real order placement through py-clob-client-v2 (CLOB V2)."""

    HOST = "https://clob.polymarket.com"
    DATA_API = "https://data-api.polymarket.com"
    CHAIN_ID = 137

    def __init__(self, cfg: dict, tracker: BookTracker):
        from py_clob_client_v2 import ClobClient

        key = os.environ.get("POLYMARKET_PRIVATE_KEY") or ""
        funder = os.environ.get("POLYMARKET_FUNDER") or None
        if not key:
            raise SystemExit(
                "live mode requires POLYMARKET_PRIVATE_KEY in .env "
                "(and POLYMARKET_FUNDER for email/browser-wallet accounts)"
            )
        self.cfg = cfg
        self.order_ttl = int(cfg["quoting"].get("order_ttl_secs", 90))
        self.rpc_url = cfg["live"].get("rpc_url")
        sig_type = int(cfg["live"]["signature_type"])
        if sig_type in (1, 2, 3) and not funder:
            raise SystemExit(
                f"live.signature_type={sig_type} requires POLYMARKET_FUNDER "
                "so orders, balances, and positions target the funding wallet "
                "(proxy / Safe / deposit wallet)"
            )
        kwargs = {"key": key, "chain_id": self.CHAIN_ID, "signature_type": sig_type}
        if funder:
            kwargs["funder"] = funder
        self.client = ClobClient(self.HOST, **kwargs)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.tracker = tracker
        self.address = funder or self.client.get_address()
        self._client_lock = threading.RLock()
        self.ws_fills_active = False
        self._open_orders: dict[str, list[RestingOrder]] = {}
        self._exit_orders: dict[str, RestingOrder] = {}
        self._markets: dict[str, Market] = {}
        self._positions: dict[str, dict] = {}
        self._token_shares: dict[str, float] = {}
        self._collateral: float = float("nan")
        self._synced = False
        self.fills_log: list[dict] = []
        self._ws_deltas: deque[tuple[float, str, float, str, float, str]] = deque()
        # record_user_fill appends on the event loop while refresh_state
        # (worker thread) iterates/trims — guard against concurrent mutation.
        self._ws_deltas_lock = threading.Lock()
        self._last_order_reconcile = 0.0
        self.metrics = None
        self.merger = None
        if cfg["live"].get("merge_enabled", False):
            try:
                from .merger import Merger
                # Deposit wallets (signature_type 3) merge gaslessly via the
                # Polymarket relayer, which authenticates with *builder* API-key
                # creds (Builders page -> API keys: apiKey/secret/passphrase).
                # Absent, type-3 merging stays off.
                builder_creds = None
                bk = os.environ.get("POLYMARKET_BUILDER_API_KEY")
                bs = os.environ.get("POLYMARKET_BUILDER_SECRET")
                bp = os.environ.get("POLYMARKET_BUILDER_PASSPHRASE")
                if bk and bs and bp:
                    builder_creds = {"key": bk, "secret": bs, "passphrase": bp}
                self.merger = Merger(
                    cfg["live"]["rpc_url"], sig_type, key, funder,
                    relayer_url=cfg["live"].get("relayer_url"),
                    builder_creds=builder_creds)
            except Exception as e:  # noqa: BLE001
                log.warning("on-chain merger unavailable: %s", e)
        log.info("live client ready (signature_type=%d, address=%s)", sig_type, self.address)

    def _gtd_expiration(self) -> int:
        return int(time.time()) + self.order_ttl + GTD_SECURITY_THRESHOLD_SECS

    def _erc20_balance(self, token_address: str, owner: str) -> float:
        import httpx

        if not self.rpc_url:
            raise RuntimeError("live.rpc_url is required for on-chain balance refresh")
        owner_arg = owner.lower().removeprefix("0x").rjust(64, "0")
        data = "0x70a08231" + owner_arg
        resp = httpx.post(
            self.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": token_address, "data": data}, "latest"],
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        return _parse_erc20_balance(payload.get("result"))

    def _sync_clob_balance(self, asset_type, token_id: str | None = None) -> None:
        """Push on-chain balances into the CLOB's server-side cache.

        REQUIRED for deposit wallets (signature_type 3): unlike EOA/proxy
        wallets, the CLOB does not auto-track a deposit wallet's chain balances,
        so until this is called the cache reads 0 and orders are rejected with
        "not enough balance / allowance: ... balance: 0". Call it for COLLATERAL
        before spending pUSD and for the CONDITIONAL token before selling it.
        Best-effort: a sync hiccup shouldn't crash the quoting loop."""
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams  # noqa: F401
            params = BalanceAllowanceParams(
                asset_type=asset_type,
                token_id=token_id or "",
                signature_type=int(self.cfg["live"]["signature_type"]),
            )
            with self._client_lock:
                self.client.update_balance_allowance(params)
        except Exception as e:  # noqa: BLE001
            log.debug("CLOB balance sync failed (%s %s): %s",
                      asset_type, (token_id or "")[:12], e)

    @staticmethod
    def _select_collateral(onchain: float | None, clob_cache: float | None) -> float | None:
        """On-chain pUSD is the source of truth for collateral; the CLOB cache
        is only a fallback. For deposit wallets the cache can be stale/zero, so
        never take max() of the two (a stale-high cache would inflate equity)."""
        if onchain is not None:
            return onchain
        return clob_cache

    def _place_buy(self, q: Quote) -> RestingOrder | None:
        from py_clob_client_v2 import OrderArgs, OrderType, Side

        try:
            with self._client_lock:
                expiration = self._gtd_expiration()
                signed = self.client.create_order(OrderArgs(
                    price=q.price, size=q.size, side=Side.BUY, token_id=q.token_id,
                    expiration=expiration,
                ))
                resp = self.client.post_order(signed, OrderType.GTD)
            oid = resp.get("orderID") or resp.get("orderId") or ""
            if oid:
                return RestingOrder(oid, q, time.time(), expiration)
        except Exception as e:  # noqa: BLE001
            log.error("order failed %s @ %.3f: %s", q.token_id[:12], q.price, e)
        return None

    def _place_sell(self, q: Quote) -> RestingOrder | None:
        from py_clob_client_v2 import AssetType, OrderArgs, OrderType, Side

        # Selling spends the conditional token; the CLOB must have a fresh view
        # of the deposit wallet's holding of it or it rejects with "balance: 0".
        self._sync_clob_balance(AssetType.CONDITIONAL, q.token_id)
        try:
            with self._client_lock:
                expiration = self._gtd_expiration()
                signed = self.client.create_order(OrderArgs(
                    price=q.price, size=q.size, side=Side.SELL, token_id=q.token_id,
                    expiration=expiration,
                ))
                resp = self.client.post_order(signed, OrderType.GTD)
            oid = resp.get("orderID") or resp.get("orderId") or ""
            if oid:
                return RestingOrder(oid, q, time.time(), expiration)
        except Exception as e:  # noqa: BLE001
            log.error("exit order failed %s @ %.3f: %s", q.token_id[:12], q.price, e)
        return None

    def _batch_cancel(self, order_ids: list[str]) -> bool:
        if not order_ids:
            return True
        try:
            with self._client_lock:
                self.client.cancel_orders(order_ids)
            return True
        except Exception as e:  # noqa: BLE001
            from py_clob_client_v2 import OrderPayload

            ok = True
            for oid in order_ids:
                try:
                    with self._client_lock:
                        self.client.cancel_order(OrderPayload(orderID=oid))
                except Exception:  # noqa: BLE001
                    ok = False
                    log.warning("cancel failed %s: %s", oid[:16], e)
            return ok

    def set_quotes(self, market: Market, quotes: list[Quote]) -> None:
        from py_clob_client_v2 import (
            OrderArgs, OrderType, PartialCreateOrderOptions, PostOrdersV2Args, Side,
        )

        self._markets[market.condition_id] = market
        desired = {q.token_id: q for q in quotes}
        now = time.time()
        kept: list[RestingOrder] = []
        to_cancel: list[str] = []

        for ro in self._open_orders.get(market.condition_id, []):
            d = desired.get(ro.quote.token_id)
            near_expiry = (ro.expiration > 0
                           and ro.expiration - now < GTD_REFRESH_MARGIN_SECS)
            if (d is not None and d.key() == ro.quote.key() and not near_expiry):
                kept.append(ro)
                desired.pop(ro.quote.token_id)
            else:
                to_cancel.append(ro.order_id)

        if not self._batch_cancel(to_cancel):
            log.warning("cancel failed in '%s' — reconciling before posting replacements",
                        market.question[:45])
            self.reconcile_orders()
            return

        placed: list[RestingOrder] = []
        if desired:
            batch_args = []
            order_map: list[Quote] = []
            options = PartialCreateOrderOptions(neg_risk=market.neg_risk)
            for q in desired.values():
                try:
                    expiration = self._gtd_expiration()
                    with self._client_lock:
                        signed = self.client.create_order(OrderArgs(
                            price=q.price, size=q.size, side=Side.BUY, token_id=q.token_id,
                            expiration=expiration,
                        ), options)
                    batch_args.append(PostOrdersV2Args(order=signed, orderType=OrderType.GTD))
                    order_map.append(q)
                except Exception as e:  # noqa: BLE001
                    log.error("order build failed %s @ %.3f: %s", q.token_id[:12], q.price, e)
            if batch_args:
                try:
                    with self._client_lock:
                        resp = self.client.post_orders(batch_args)
                    orders = resp if isinstance(resp, list) else resp.get("orders", [resp])
                    for i, item in enumerate(orders):
                        oid = ""
                        if isinstance(item, dict):
                            oid = item.get("orderID") or item.get("orderId") or ""
                        if oid and i < len(order_map):
                            placed.append(RestingOrder(
                                oid, order_map[i], now, self._gtd_expiration()))
                except Exception as e:  # noqa: BLE001
                    log.error("batch post failed; reconciling instead of retrying blindly: %s", e)
                    self.reconcile_orders()
                    return
                if len(placed) < len(batch_args):
                    # The post succeeded (no exception) but we parsed fewer order
                    # IDs than orders sent — the unparsed orders may still be
                    # resting on the exchange. Trusting empty local state here
                    # would re-post them next cycle (duplicate stacking), so
                    # rebuild from exchange truth instead.
                    log.warning("batch post parsed %d/%d order IDs in '%s' — "
                                "reconciling to avoid duplicate orders",
                                len(placed), len(batch_args), market.question[:45])
                    self.reconcile_orders()
                    return

        self._open_orders[market.condition_id] = kept + placed
        if self.metrics:
            self.metrics.record_quotes(market.condition_id, quotes)

    def cancel_all(self) -> None:
        ok = True
        try:
            with self._client_lock:
                self.client.cancel_all()
        except Exception as e:  # noqa: BLE001
            ok = False
            log.error("cancel_all failed: %s", e)
        if ok:
            self._open_orders.clear()
            self._exit_orders.clear()
        else:
            self.reconcile_orders()

    def cancel_quotes(self) -> None:
        ids = [ro.order_id for orders in self._open_orders.values() for ro in orders]
        if self._batch_cancel(ids):
            self._open_orders.clear()
        else:
            self.reconcile_orders()

    def cancel_quotes_for_market(self, market: Market) -> None:
        cid = market.condition_id
        ids = [ro.order_id for ro in self._open_orders.get(cid, [])]
        if self._batch_cancel(ids):
            self._open_orders.pop(cid, None)
        else:
            self.reconcile_orders()

    def open_quotes(self, market: Market) -> list[Quote]:
        return [ro.quote for ro in self._open_orders.get(market.condition_id, [])]

    def set_exit(self, market: Market, quote: Quote | None) -> None:
        cid = market.condition_id
        cur = self._exit_orders.get(cid)
        now = time.time()
        if (cur is not None and quote is not None and cur.quote.key() == quote.key()
                and cur.expiration - now >= GTD_REFRESH_MARGIN_SECS):
            return
        if cur is not None:
            if self._batch_cancel([cur.order_id]):
                self._exit_orders.pop(cid, None)
            else:
                self.reconcile_orders()
                return
        if quote is None:
            return
        self._markets[cid] = market
        ro = self._place_sell(quote)
        if ro:
            self._exit_orders[cid] = ro
            log.info("EXIT SELL resting in '%s': %.0f @ %.3f",
                     market.question[:40], quote.size, quote.price)

    def exit_quote(self, market: Market) -> Quote | None:
        cur = self._exit_orders.get(market.condition_id)
        return cur.quote if cur else None

    def taker_buy(self, market: Market, token_id: str, size: float, max_price: float) -> float:
        from py_clob_client_v2 import (
            AssetType, MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side,
        )

        size = round(size, 2)
        # Hedge buys spend pUSD collateral; refresh the CLOB's deposit-wallet view.
        self._sync_clob_balance(AssetType.COLLATERAL)
        # A marketable FAK buy is validated by the backend as a *market* buy, whose
        # maker (collateral) amount must round to <=2 decimals. create_order(price,
        # size) sends maker = price*size, which carries up to 4 decimals and is
        # rejected ("invalid amounts ... maker amount supports a max accuracy of 2
        # decimals"). The market-order builder takes the spend amount directly and
        # rounds it correctly, so quote in collateral terms instead.
        amount = round(size * max_price, 2)
        if amount <= 0:
            return 0.0
        try:
            with self._client_lock:
                signed = self.client.create_market_order(MarketOrderArgs(
                    token_id=token_id, amount=amount, side=Side.BUY,
                    price=max_price, order_type=OrderType.FAK,
                ), PartialCreateOrderOptions(neg_risk=market.neg_risk))
                resp = self.client.post_order(signed, OrderType.FAK)
        except Exception as e:  # noqa: BLE001
            log.error("taker order failed %s @ %.3f: %s", token_id[:12], max_price, e)
            return 0.0
        filled = _parse_fill_amount(resp, size)
        if filled > 0 and self.metrics:
            self.metrics.record_hedge(market.condition_id, max_price, filled)
        return filled

    def _apply_fill_to_orders(self, token_id: str, size: float, side: str) -> None:
        """Decrement resting orders on fill."""
        if side == "BUY":
            for cid, orders in list(self._open_orders.items()):
                new_orders = []
                for ro in orders:
                    if ro.quote.token_id != token_id or size <= 0:
                        new_orders.append(ro)
                        continue
                    order_size = ro.quote.size
                    remaining = order_size - size
                    if remaining > 0.01:
                        ro.quote = Quote(token_id, ro.quote.price, remaining)
                        new_orders.append(ro)
                    size = max(0.0, size - order_size)
                self._open_orders[cid] = new_orders
        elif side == "SELL":
            for cid, ro in list(self._exit_orders.items()):
                if ro.quote.token_id == token_id:
                    remaining = ro.quote.size - size
                    if remaining <= 0.01:
                        self._exit_orders.pop(cid, None)
                    else:
                        ro.quote = Quote(token_id, ro.quote.price, remaining)

    def record_user_fill(self, token_id: str, side: str, price: float,
                         size: float, taker: bool = False) -> None:
        if size <= 0:
            return
        ts = time.time()
        delta = size if side == "BUY" else -size
        self._token_shares[token_id] = max(
            0.0, self._token_shares.get(token_id, 0.0) + delta)
        market = next(
            (m for m in self._markets.values()
             if token_id in (m.yes_token, m.no_token)), None)
        if market is None:
            return
        with self._ws_deltas_lock:
            self._ws_deltas.append((ts, token_id, delta, side, price, market.condition_id))
        d = self._positions.setdefault(
            market.condition_id, {"yes": 0.0, "no": 0.0, "value": 0.0})
        key = "yes" if token_id == market.yes_token else "no"
        d[key] = max(0.0, d[key] + delta)
        if not taker:
            self._apply_fill_to_orders(token_id, size, side)
        entry = {
            "ts": ts, "cid": market.condition_id,
            "market": market.question[:50],
            "side": "YES" if key == "yes" else "NO",
            "token": token_id, "price": price, "size": size,
        }
        if taker:
            entry["taker"] = True
        if side == "SELL":
            entry["exit"] = True
        self.fills_log.append(entry)
        self.fills_log = self.fills_log[-500:]
        if self.metrics:
            self.metrics.record_fill(entry)
        log.info("LIVE FILL (ws) %s %s %s %.1f @ %.3f",
                 market.question[:40], side, entry["side"], size, price)

    def reconcile_orders(self) -> None:
        """Rebuild local order state from exchange truth."""
        try:
            with self._client_lock:
                remote = self.client.get_open_orders()
        except Exception as e:  # noqa: BLE001
            log.warning("order reconcile failed: %s", e)
            return
        by_cid: dict[str, list[RestingOrder]] = {}
        exit_by_cid: dict[str, RestingOrder] = {}
        for o in remote:
            oid = o.get("id") or o.get("orderID") or o.get("orderId") or ""
            token = str(o.get("asset_id") or o.get("assetId") or "")
            if not oid or not token:
                continue
            market = next(
                (m for m in self._markets.values()
                 if token in (m.yes_token, m.no_token)), None)
            if market is None:
                continue
            try:
                price = float(o.get("price") or 0)
                orig = float(o.get("original_size") or o.get("size") or 0)
                matched = float(o.get("size_matched") or o.get("sizeMatched") or 0)
                remaining = max(0.0, orig - matched)
            except (TypeError, ValueError):
                continue
            if remaining <= 0:
                continue
            side = str(o.get("side") or "").upper()
            exp = int(o.get("expiration") or 0)
            ro = RestingOrder(oid, Quote(token, price, remaining), time.time(), exp)
            cid = market.condition_id
            if side == "SELL":
                exit_by_cid[cid] = ro
            else:
                by_cid.setdefault(cid, []).append(ro)
        self._open_orders = by_cid
        self._exit_orders = exit_by_cid

    def refresh_state(self) -> None:
        import httpx

        poll_start = time.time()
        try:
            resp = httpx.get(
                f"{self.DATA_API}/positions",
                params={"user": self.address, "limit": 500},
                timeout=10.0,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as e:  # noqa: BLE001
            log.warning("position refresh failed: %s", e)
            return

        positions: dict[str, dict] = {}
        token_shares: dict[str, float] = {}
        for r in rows:
            try:
                size = float(r.get("size") or 0)
                if size <= 0:
                    continue
                cid = str(r.get("conditionId") or "")
                token = str(r.get("asset") or "")
                cur = float(r.get("curPrice") or 0)
                outcome_index = int(r.get("outcomeIndex") or 0)
            except (TypeError, ValueError):
                continue
            m = self._markets.get(cid)
            is_yes = (token == m.yes_token) if m else (outcome_index == 0)
            d = positions.setdefault(cid, {"yes": 0.0, "no": 0.0, "value": 0.0})
            d["yes" if is_yes else "no"] += size
            d["value"] += size * cur
            token_shares[token] = token_shares.get(token, 0.0) + size

        if self._synced and not self.ws_fills_active:
            now = time.time()
            for token, shares in token_shares.items():
                gained = shares - self._token_shares.get(token, 0.0)
                if gained <= 1e-9:
                    continue
                market = next(
                    (m for m in self._markets.values()
                     if token in (m.yes_token, m.no_token)), None)
                if market is None:
                    continue
                entry = {
                    "ts": now, "cid": market.condition_id,
                    "market": market.question[:50],
                    "side": "YES" if token == market.yes_token else "NO",
                    "token": token, "size": gained,
                }
                for ro in self._open_orders.get(market.condition_id, []):
                    if ro.quote.token_id == token:
                        entry["price"] = ro.quote.price
                        break
                self.fills_log.append(entry)
                if self.metrics:
                    self.metrics.record_fill(entry)
                log.info("LIVE FILL detected %s %s +%.1f shares",
                         market.question[:40],
                         "YES" if token == market.yes_token else "NO", gained)
        self.fills_log = self.fills_log[-500:]

        post_poll: dict[str, float] = {}
        post_poll_cid: dict[tuple[str, str], float] = {}
        with self._ws_deltas_lock:
            while self._ws_deltas and self._ws_deltas[0][0] <= poll_start:
                self._ws_deltas.popleft()
            deltas = list(self._ws_deltas)
        for ts, token, delta, _side, _price, cid in deltas:
            if ts <= poll_start:
                continue
            post_poll[token] = post_poll.get(token, 0.0) + delta
            m = self._markets.get(cid)
            if m:
                key = "yes" if token == m.yes_token else "no"
                post_poll_cid[(cid, key)] = post_poll_cid.get((cid, key), 0.0) + delta

        self._positions = positions
        self._token_shares = dict(token_shares)
        for (cid, key), delta in post_poll_cid.items():
            d = self._positions.setdefault(cid, {"yes": 0.0, "no": 0.0, "value": 0.0})
            d[key] = max(0.0, d[key] + delta)
        for token, delta in post_poll.items():
            self._token_shares[token] = max(0.0, self._token_shares.get(token, 0.0) + delta)

        self._synced = True

        if poll_start - self._last_order_reconcile >= ORDER_RECONCILE_SECONDS:
            self._last_order_reconcile = poll_start
            self.reconcile_orders()

        # On-chain pUSD held by the wallet is the source of truth for collateral
        # (and thus equity, sizing, and loss limits). The CLOB's balance cache is
        # only used as a fallback: for deposit wallets it can be stale/zero unless
        # we push it an update, and taking max() of a stale-high cache and the
        # real balance inflates equity. We still push the cache an update so the
        # CLOB admits our BUY orders.
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        self._sync_clob_balance(AssetType.COLLATERAL)
        onchain = clob_cache = None
        try:
            onchain = self._erc20_balance(PUSD, self.address)
        except Exception as e:  # noqa: BLE001
            log.warning("pUSD balance refresh failed: %s", e)
        try:
            with self._client_lock:
                bal = self.client.get_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.COLLATERAL,
                        signature_type=int(self.cfg["live"]["signature_type"]),
                    )
                )
            clob_cache = float(bal.get("balance") or 0) / USDC_DECIMALS
        except Exception as e:  # noqa: BLE001
            log.warning("CLOB balance refresh failed: %s", e)
        picked = self._select_collateral(onchain, clob_cache)
        if picked is not None:
            self._collateral = picked

    def _yes_mid(self, market: Market) -> float | None:
        book = self.tracker.books.get(market.yes_token)
        return book.mid if book else None

    def position_tokens(self) -> list[str]:
        tokens = []
        for cid in self._positions:
            m = self._markets.get(cid)
            if m:
                tokens.extend((m.yes_token, m.no_token))
        return tokens

    def equity(self) -> float:
        if self._collateral != self._collateral or not self._synced:
            return float("nan")
        total = self._collateral
        for cid, d in self._positions.items():
            m = self._markets.get(cid)
            mid = self._yes_mid(m) if m else None
            if mid is not None:
                total += d["yes"] * mid + d["no"] * (1 - mid)
            else:
                total += d["value"]
        return total

    def net_yes_exposure_usd(self, market: Market) -> float:
        d = self._positions.get(market.condition_id)
        if d is None:
            return 0.0
        mid = self._yes_mid(market)
        if mid is None:
            mid = 0.5
        return d["yes"] * mid - d["no"] * (1 - mid)

    def unpaired_shares(self, market: Market) -> float:
        d = self._positions.get(market.condition_id)
        if d is None:
            return 0.0
        return d["yes"] - d["no"]

    def held_markets(self) -> list[Market]:
        return [self._markets[cid] for cid in self._positions if cid in self._markets]

    def total_inventory_usd(self) -> float:
        total = 0.0
        for cid, d in self._positions.items():
            m = self._markets.get(cid)
            mid = self._yes_mid(m) if m else None
            if mid is None:
                mid = 0.5
            total += abs(d["yes"] * mid - d["no"] * (1 - mid))
        return total

    def merge_pairs(self, min_pairs: float) -> None:
        if self.merger is None or self.merger.disabled or not self._synced:
            return
        for cid, d in list(self._positions.items()):
            pairs = float(int(min(d["yes"], d["no"])))
            if pairs < min_pairs:
                continue
            m = self._markets.get(cid)
            if m is None:
                continue
            log.info("merging %.0f pairs in '%s' (recovers $%.0f)",
                     pairs, m.question[:40], pairs)
            if self.merger.merge(m.condition_id, m.neg_risk, pairs):
                d["yes"] -= pairs
                d["no"] -= pairs
                self._token_shares[m.yes_token] = max(
                    0.0, self._token_shares.get(m.yes_token, 0.0) - pairs)
                self._token_shares[m.no_token] = max(
                    0.0, self._token_shares.get(m.no_token, 0.0) - pairs)
                if self.metrics:
                    self.metrics.record_merge(cid, pairs)

    def accrue_rewards(self, usd: float) -> None:
        if self.metrics:
            self.metrics.record_est_reward(usd)

    def check_crossed_books(self) -> None:
        """Detect resting orders that look crossed (should have filled) and
        force an order reconcile on the next refresh_state. Runs on the event
        loop, so it must not block — it only flags, never calls the network."""
        for orders in self._open_orders.values():
            for ro in orders:
                book = self.tracker.books.get(ro.quote.token_id)
                ask = book.best_ask if book else None
                if ask is not None and ask <= ro.quote.price:
                    log.warning("live bid appears crossed; forcing order reconcile")
                    self._last_order_reconcile = 0.0
                    return
        for ro in self._exit_orders.values():
            book = self.tracker.books.get(ro.quote.token_id)
            bid = book.best_bid if book else None
            if bid is not None and bid >= ro.quote.price:
                log.warning("live exit ask appears crossed; forcing order reconcile")
                self._last_order_reconcile = 0.0
                return
