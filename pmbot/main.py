"""Orchestrator CLI.

    python -m pmbot.main scan         # show current best reward markets
    python -m pmbot.main run          # run the market maker (paper or live per config)
    python -m pmbot.main report       # daily PnL decomposition from metrics.db
    python -m pmbot.main trades       # recent fill log from metrics.db
    python -m pmbot.main performance  # per-market breakdown for tuning
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import logging
import time
from datetime import datetime, timezone

import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import gamma, strategy
from .books import BookTracker
from .brokers import LiveBroker, PaperBroker
from .controller import AdaptiveController
from .metrics import MetricsStore
from .risk import MarketGuards, MarkoutTracker, RiskAction, RiskManager

console = Console()
log = logging.getLogger("pmbot")

LOOP_SECONDS = 2.0
REWARD_SAMPLE_SECONDS = 60.0
STATUS_SECONDS = 30.0
MINUTES_PER_DAY = 1440.0
POSITION_REFRESH_SECONDS = 12.0
MERGE_CHECK_SECONDS = 60.0
FLATTEN_RETRY_SECONDS = 15.0
MIN_TAKER_SHARES = 5.0
REALIZED_REWARD_FETCH_SECONDS = 3600.0
SCAN_RETRY_SECONDS = 60.0


def hours_to_end(market: gamma.Market, now: float) -> float | None:
    if market.end_date is None:
        return None
    return (market.end_date.timestamp() - now) / 3600.0


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def cmd_scan(cfg: dict) -> None:
    markets = gamma.scan(cfg)
    table = Table(title="Top reward markets (pool ÷ liquidity)")
    for col in ("Market", "Mid", "Pool/day", "Liquidity", "Fee", "Min size", "Band", "Score"):
        table.add_column(col)
    for m in markets:
        table.add_row(
            m.question[:55], f"{m.mid_hint:.2f}", f"${m.daily_pool:,.0f}",
            f"${m.liquidity:,.0f}", f"{m.fee_bps}bps",
            f"{m.min_size:.0f} sh", f"{m.max_spread_cents}c", f"{m.score:.3f}",
        )
    console.print(table)


def _metrics_store(cfg: dict) -> MetricsStore:
    m = cfg.get("metrics") or {}
    return MetricsStore(m.get("db_path", "data/metrics.db"),
                        trades_log=m.get("trades_log"))


def cmd_report(cfg: dict) -> None:
    store = _metrics_store(cfg)
    report = store.daily_report()
    store.close()
    table = Table(title=f"PnL report — {report['date']}")
    table.add_column("Component")
    table.add_column("USD")
    for key, label in [
        ("spread_capture_usd", "Spread capture (merges)"),
        ("hedge_cost_usd", "Forced hedge cost"),
        ("fees_usd", "Fees paid"),
        ("est_rewards_usd", "Est. rewards"),
        ("realized_rewards_usd", "Realized rewards"),
        ("equity_pnl_usd", "Equity PnL"),
    ]:
        table.add_row(label, f"${report[key]:+.4f}")
    table.add_row("Maker fills", str(report["maker_fills"]))
    table.add_row("In-band uptime", f"{report['uptime_pct']:.1f}%")
    console.print(table)


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def cmd_trades(cfg: dict, limit: int, hours: float | None,
               export_csv: str | None) -> None:
    store = _metrics_store(cfg)
    since = time.time() - hours * 3600 if hours is not None else None
    fills = store.recent_fills(limit=limit, since_ts=since)
    store.close()
    if export_csv:
        with open(export_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_utc", "market", "type", "side", "price", "size",
                        "merged", "fee_usd", "cid"])
            for fill in reversed(fills):
                if fill["exit"]:
                    kind = "exit"
                elif fill["taker"]:
                    kind = "taker"
                else:
                    kind = "maker"
                w.writerow([
                    _fmt_ts(fill["ts"]), fill["market"], kind, fill["side"],
                    f"{fill['price']:.4f}", f"{fill['size']:.1f}",
                    f"{fill['merged']:.1f}", f"{fill['fee']:.4f}", fill["cid"],
                ])
        console.print(f"exported {len(fills)} fills to {export_csv}")
        return
    if not fills:
        console.print("no fills recorded yet — run the bot in paper mode first")
        return
    table = Table(title="Recent fills")
    for col in ("Time (UTC)", "Market", "Type", "Side", "Price", "Size", "Merged", "Fee"):
        table.add_column(col)
    for fill in fills:
        if fill["exit"]:
            kind = "exit"
        elif fill["taker"]:
            kind = "taker"
        else:
            kind = "maker"
        table.add_row(
            _fmt_ts(fill["ts"]),
            fill["market"][:40],
            kind,
            fill["side"],
            f"{fill['price']:.3f}",
            f"{fill['size']:.0f}",
            f"{fill['merged']:.0f}" if fill["merged"] else "—",
            f"${fill['fee']:.2f}" if fill["fee"] else "—",
        )
    console.print(table)


def cmd_performance(cfg: dict, date: str | None) -> None:
    store = _metrics_store(cfg)
    report = store.performance_report(date)
    store.close()
    summary = report["summary"]
    console.print(
        f"[bold]Session summary — {report['date']}[/]  "
        f"equity PnL ${summary['equity_pnl_usd']:+.2f}  "
        f"spread ${summary['spread_capture_usd']:+.2f}  "
        f"hedges ${summary['hedge_cost_usd']:+.2f}  "
        f"fees ${summary['fees_usd']:+.2f}  "
        f"est. rewards ${summary['est_rewards_usd']:+.4f}  "
        f"maker fills {summary['maker_fills']}  "
        f"uptime {summary['uptime_pct']:.1f}%"
    )
    markets = report["markets"]
    if not markets:
        console.print("no per-market activity yet — run the bot in paper mode first")
        return
    table = Table(title=f"Per-market performance — {report['date']}")
    for col in ("Market", "Maker", "Taker", "Exit", "Merged", "Hedge $",
                "Fees", "Markout", "Uptime"):
        table.add_column(col)
    for m in markets:
        markout = "—"
        if m["markout_cents"] is not None:
            markout = f"{m['markout_cents']:+.1f}c (n={m['markout_n']})"
        table.add_row(
            (m["market"] or m["cid"][:12])[:40],
            str(m["maker_fills"]),
            str(m["taker_fills"]),
            str(m["exits"]),
            f"${m['merged_pairs']:.0f}",
            f"${m['hedge_cost_usd']:.2f}",
            f"${m['fees_usd']:.2f}",
            markout,
            f"{m['uptime_pct']:.0f}%",
        )
    console.print(table)
    console.print(
        "\n[dim]Use markout + uptime to drop toxic markets; "
        "merged/hedge ratio shows spread capture vs forced pairing cost.[/]"
    )


class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.paper = cfg["mode"] != "live"
        self.markets: list[gamma.Market] = []
        self.tracker: BookTracker | None = None
        self.broker = None
        self.userfeed = None
        self.risk: RiskManager | None = None
        self.guards = MarketGuards(cfg)
        self.markouts = MarkoutTracker(cfg)
        self.metrics = _metrics_store(cfg)
        self.controller = AdaptiveController(cfg, self.guards, self.markouts,
                                             self.metrics)
        self._token_market: dict[str, gamma.Market] = {}
        self._size_factors: dict[str, float] = {}
        self._last_scan = 0.0
        self._last_reward_sample = 0.0
        self._last_status = 0.0
        self._last_pos_refresh = 0.0
        self._last_merge_check = 0.0
        self._last_realized_reward = 0.0
        self._merge_task: asyncio.Task | None = None
        self._over_since: dict[str, float] = {}
        self._last_flatten: dict[str, float] = {}
        self._scale = 1.0
        self._was_paused = False
        # Event-driven quote pulls: guards fire these between loop ticks so we
        # don't stay quoted on an endangered side for up to LOOP_SECONDS.
        self._pull_tasks: set[asyncio.Task] = set()
        self._market_locks: dict[str, asyncio.Lock] = {}
        self.guards.on_trip = self._schedule_market_pull
        self.guards.on_side_block = self._schedule_side_pull

    async def run(self) -> None:
        while True:
            await self._rescan(initial=True)
            if self.markets:
                break
            log.warning("scanner found no eligible markets — retrying in %.0fs "
                        "(loosen config filters to match more markets)",
                        SCAN_RETRY_SECONDS)
            await asyncio.sleep(SCAN_RETRY_SECONDS)
        assert self.broker and self.risk
        try:
            while True:
                await asyncio.sleep(LOOP_SECONDS)
                now = time.time()
                if now - self._last_scan > self.cfg["scanner"]["refresh_minutes"] * 60:
                    await self._rescan()
                self.broker.check_crossed_books()
                if not self.paper and now - self._last_pos_refresh >= POSITION_REFRESH_SECONDS:
                    await asyncio.to_thread(self.broker.refresh_state)
                    self._last_pos_refresh = now
                if not self.paper and now - self._last_merge_check >= MERGE_CHECK_SECONDS:
                    self._last_merge_check = now
                    if self._merge_task is None or self._merge_task.done():
                        min_pairs = float(self.cfg["live"].get("merge_min_pairs", 20))
                        self._merge_task = asyncio.create_task(
                            asyncio.to_thread(self.broker.merge_pairs, min_pairs))
                if (not self.paper and now - self._last_realized_reward
                        >= REALIZED_REWARD_FETCH_SECONDS):
                    self._last_realized_reward = now
                    await asyncio.to_thread(
                        self.metrics.fetch_realized_rewards, self.broker.client)

                equity = self.broker.equity()
                self.controller.maybe_apply(now, equity)
                self._scale = self.risk.scale(equity)
                action = self.risk.check(equity, self.broker.total_inventory_usd(),
                                         self._scale)
                self.metrics.record_equity(equity, self.broker.total_inventory_usd())

                if action == RiskAction.KILL:
                    break

                if action in (RiskAction.PAUSE_DAY, RiskAction.PAUSE_QUOTES):
                    if not self._was_paused:
                        await self._broker_call(self.broker.cancel_quotes)
                        self._was_paused = True
                    for m in self.markets:
                        self.metrics.sample_uptime(m.condition_id, False)
                    await self._manage_inventory(now)
                    continue

                self._was_paused = False
                await self._quote_all()
                await self._manage_inventory(now)

                if now - self._last_reward_sample >= REWARD_SAMPLE_SECONDS:
                    self._sample_rewards()
                    self._last_reward_sample = now
                if now - self._last_status >= STATUS_SECONDS:
                    self._print_status()
                    self._last_status = now
        finally:
            log.info("shutting down — cancelling all orders")
            for task in list(self._pull_tasks):
                task.cancel()
            if self._pull_tasks:
                await asyncio.gather(*self._pull_tasks, return_exceptions=True)
            if self.userfeed:
                await self.userfeed.stop()
            if self._merge_task and not self._merge_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await self._merge_task
            await self._broker_call(self.broker.cancel_all)
            if self.tracker:
                await self.tracker.stop()
            self._print_status()
            self.metrics.close()

    async def _broker_call(self, fn, *args):
        """Dispatch broker order ops off the event loop in live mode."""
        if self.paper:
            return fn(*args)
        return await asyncio.to_thread(fn, *args)

    def _market_lock(self, cid: str) -> asyncio.Lock:
        lock = self._market_locks.get(cid)
        if lock is None:
            lock = self._market_locks[cid] = asyncio.Lock()
        return lock

    async def _set_quotes_locked(self, market: gamma.Market,
                                 quotes: list[strategy.Quote]) -> None:
        """Serialize quote ops per market so an event-driven pull cannot race
        a concurrent replace from the main loop."""
        async with self._market_lock(market.condition_id):
            await self._broker_call(self.broker.set_quotes, market, quotes)

    def _spawn_pull(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (tests / shutdown)
            coro.close()
            return
        self._pull_tasks.add(task)
        task.add_done_callback(self._pull_tasks.discard)

    def _schedule_market_pull(self, cid: str) -> None:
        self._spawn_pull(self._pull_market_quotes(cid))

    def _schedule_side_pull(self, token_id: str) -> None:
        self._spawn_pull(self._pull_side_quote(token_id))

    async def _pull_market_quotes(self, cid: str) -> None:
        """Immediately cancel all quotes in a guard-tripped market."""
        if self.broker is None:
            return
        m = next((mm for mm in self.markets if mm.condition_id == cid), None)
        if m is None:
            return
        async with self._market_lock(cid):
            if not self.broker.open_quotes(m):
                return
            log.warning("guard trip — pulling quotes from '%s' now", m.question[:45])
            self.metrics.sample_uptime(cid, False)
            await self._broker_call(self.broker.set_quotes, m, [])

    async def _pull_side_quote(self, token_id: str) -> None:
        """Immediately cancel the quote on a blocked side."""
        if self.broker is None:
            return
        m = self._token_market.get(token_id)
        if m is None:
            return
        async with self._market_lock(m.condition_id):
            current = self.broker.open_quotes(m)
            remaining = [q for q in current if q.token_id != token_id]
            if len(remaining) == len(current):
                return
            log.warning("side block — pulling %s bid in '%s' now",
                        "YES" if token_id == m.yes_token else "NO",
                        m.question[:45])
            await self._broker_call(self.broker.set_quotes, m, remaining)

    async def _rescan(self, initial: bool = False) -> None:
        log.info("scanning for reward markets…")
        markets = await asyncio.to_thread(gamma.scan, self.cfg)
        if not markets:
            if not initial:
                log.warning("rescan found no markets; keeping current set")
            self._last_scan = time.time()
            return

        old_markets = list(self.markets)
        new_cids = {m.condition_id for m in markets}
        old_cids = {m.condition_id for m in old_markets}
        new_tokens = {t for m in markets for t in (m.yes_token, m.no_token)}
        old_tokens = {t for m in old_markets for t in (m.yes_token, m.no_token)}
        set_changed = new_cids != old_cids

        for m in markets:
            log.info("quoting: %s  (pool $%.0f/day, score %.3f)",
                     m.question[:60], m.daily_pool, m.score)

        self.markets = markets
        self._token_market = {}
        for m in markets:
            self._token_market[m.yes_token] = m
            self._token_market[m.no_token] = m

        if self.tracker and not initial and not set_changed:
            self._last_scan = time.time()
            self._compute_size_factors()
            return

        carry_books: dict = {}
        if self.broker and not initial:
            for old_m in old_markets:
                if old_m.condition_id in old_cids - new_cids:
                    async with self._market_lock(old_m.condition_id):
                        if hasattr(self.broker, "cancel_quotes_for_market"):
                            await self._broker_call(
                                self.broker.cancel_quotes_for_market, old_m)
                        else:
                            await self._broker_call(
                                self.broker.set_quotes, old_m, [])

        token_ids = list(new_tokens)
        if self.tracker:
            carry_books = {
                t: self.tracker.books[t]
                for t in new_tokens & old_tokens
                if t in self.tracker.books
            }
            await self.tracker.stop()
            if self.broker:
                for t in self.broker.position_tokens():
                    if t not in token_ids:
                        token_ids.append(t)

        self.tracker = BookTracker(token_ids, carry=carry_books)

        if initial:
            if self.paper:
                p = self.cfg.get("paper") or {}
                self.broker = PaperBroker(
                    self.cfg["capital_usd"], self.tracker,
                    latency_secs=float(p.get("order_latency_ms", 300)) / 1000.0)
                self.risk = RiskManager(self.cfg, self.cfg["capital_usd"])
            else:
                self.broker = LiveBroker(self.cfg, self.tracker)
                await asyncio.to_thread(self.broker.refresh_state)
                self._last_pos_refresh = time.time()
                self.risk = RiskManager(self.cfg, self.broker.equity())
                from .userfeed import UserFeed
                self.userfeed = UserFeed(self.broker)
                self.userfeed.start()
        else:
            self.broker.tracker = self.tracker
            if self.paper:
                self.tracker.on_trade(self.broker._on_trade)

        self.broker.metrics = self.metrics
        self.tracker.on_trade(self._on_market_trade)
        await self.tracker.start()
        self._last_scan = time.time()
        self._compute_size_factors()

    def _compute_size_factors(self) -> None:
        if not self.tracker:
            return
        self._size_factors = strategy.compute_size_factors(
            self.markets,
            self.tracker.books,
            self.broker.open_quotes,
            self.cfg,
        )

    async def _on_market_trade(self, token_id: str, price: float,
                               side: str, size: float) -> None:
        market = self._token_market.get(token_id)
        if market is not None:
            self.guards.record_trade(market, token_id, side, size, time.time())

    async def _quote_all(self) -> None:
        r = self.cfg["risk"]
        max_inv = r["max_inventory_usd_per_market"]
        derisk_h = r["derisk_hours_before_end"]
        exit_h = r["exit_hours_before_end"]
        widen_max = r["derisk_widen_cents"] / 100.0
        max_stale = self.cfg["guards"]["max_book_staleness_secs"]
        now = time.time()
        self.guards.check_fills(self.broker.fills_log, now)
        self.markouts.ingest(self.broker.fills_log)
        for mo in self.markouts.resolve(self._token_mid, now):
            self.metrics.record_markout(mo)
        for cid, avg_cents, n in self.markouts.toxic_markets():
            m = next((mm for mm in self.markets if mm.condition_id == cid), None)
            self.guards.trip_market(
                cid, now, f"avg markout {avg_cents:+.1f}c over {n} fills",
                m.question if m else cid)
            self.markouts.reset_market(cid)
        managed = {m.condition_id: m for m in self.markets}
        for m in self.broker.held_markets():
            managed.setdefault(m.condition_id, m)
        all_markets = list(managed.values())
        net_exp = self.broker.net_yes_exposure_usd
        # Decide all markets first, then dispatch order ops concurrently so
        # markets late in the iteration aren't quoted on stale books while
        # earlier ones complete their REST round trips.
        updates: list[tuple[gamma.Market, list[strategy.Quote]]] = []

        for m in self.markets:
            h = hours_to_end(m, now)
            if h is not None and h <= exit_h:
                if self.broker.open_quotes(m):
                    log.warning("'%s' resolves in %.1fh — exiting market", m.question[:45], h)
                    updates.append((m, []))
                continue
            yes_book = self.tracker.books[m.yes_token]
            no_book = self.tracker.books[m.no_token]
            if yes_book.mid is not None:
                self.guards.record_mid(m.condition_id, yes_book.mid, now, m.question)
            feed_age = self.tracker.feed_age(now)
            book_age = now - min(yes_book.updated_ts, no_book.updated_ts)
            if strategy.book_feed_stale(feed_age, book_age, max_stale):
                self.metrics.sample_uptime(m.condition_id, False)
                if self.broker.open_quotes(m):
                    log.warning("feed/book stale (feed %.0fs, book %.0fs) — "
                                "pulling quotes from '%s'",
                                feed_age, book_age, m.question[:45])
                    updates.append((m, []))
                continue
            band = m.max_spread_cents / 100.0
            max_spread_mult = float(
                self.cfg["quoting"].get("max_book_spread_mult_of_band", 3.0))
            if (not strategy.book_is_quotable(yes_book, band, max_spread_mult)
                    or not strategy.book_is_quotable(no_book, band, max_spread_mult)):
                self.metrics.sample_uptime(m.condition_id, False)
                if self.broker.open_quotes(m):
                    log.warning("book not quotable on both sides — pulling '%s'",
                                m.question[:45])
                    updates.append((m, []))
                continue
            if not self.guards.allow(m.condition_id, now):
                self.metrics.sample_uptime(m.condition_id, False)
                if self.broker.open_quotes(m):
                    updates.append((m, []))
                continue
            if not self.risk.theme_quoting_ok(m, all_markets, net_exp, self._scale):
                self.metrics.sample_uptime(m.condition_id, False)
                if self.broker.open_quotes(m):
                    log.warning("theme inventory cap — not quoting '%s'",
                                m.question[:45])
                    updates.append((m, []))
                continue
            derisk_frac = 1.0
            if h is not None and h <= derisk_h:
                derisk_frac = max(0.25, (h - exit_h) / max(derisk_h - exit_h, 1e-9))
            eff_max_inv = max_inv * derisk_frac * self._scale
            widen = (1.0 - derisk_frac) * widen_max
            exposure = net_exp(m)
            if (not self.risk.market_inventory_ok(exposure, eff_max_inv)
                    or self.risk.theme_at_cap(m, all_markets, net_exp, self._scale)):
                exposure = eff_max_inv if exposure > 0 else -eff_max_inv
            fade_yes, fade_no = self._fades(m, now)
            flow_yes, flow_no = self.guards.check_flow(m, now)
            flow_imb = self.guards.flow_imbalance(m, now)
            markout_avg = self.markouts.market_avg(m.condition_id)
            size_factor = self._size_factors.get(m.condition_id, 1.0)
            desired = strategy.compute_quotes(
                m, yes_book, exposure, self.cfg, eff_max_inv,
                fade_yes=fade_yes + widen + flow_yes,
                fade_no=fade_no + widen + flow_no,
                scale=self._scale,
                flow_imbalance=flow_imb,
                markout_avg=markout_avg,
                size_factor=size_factor,
            )
            desired = [q for q in desired if self.guards.allow_side(q.token_id, now)]
            current = self.broker.open_quotes(m)
            final = strategy.reconcile_quotes(
                current, desired, self.cfg["quoting"]["requote_move_cents"])
            in_band = (len(final) == 2
                       and any(q.token_id == m.yes_token for q in final)
                       and any(q.token_id == m.no_token for q in final))
            self.metrics.sample_uptime(m.condition_id, in_band)
            if {q.key() for q in final} != {q.key() for q in current}:
                updates.append((m, final))

        if updates:
            await asyncio.gather(
                *(self._set_quotes_locked(m, q) for m, q in updates))

    async def _manage_inventory(self, now: float) -> None:
        quoted = {m.condition_id for m in self.markets}
        managed = {m.condition_id: m for m in self.markets}
        for m in self.broker.held_markets():
            managed.setdefault(m.condition_id, m)
        if not managed:
            return
        # Exits/hedges on different markets are independent — run them
        # concurrently so one slow hedge doesn't delay the others.
        await asyncio.gather(*(
            self._manage_market_inventory(cid, m, managed, quoted, now)
            for cid, m in managed.items()))

    async def _manage_market_inventory(self, cid: str, m: gamma.Market,
                                       managed: dict[str, gamma.Market],
                                       quoted: set[str], now: float) -> None:
        r = self.cfg["risk"]
        threshold = r["flatten_threshold_usd"] * self._scale
        wait = r["flatten_after_secs"]
        max_spread = r["flatten_max_spread_cents"] / 100.0
        exit_h = r["exit_hours_before_end"]
        passive = bool(r.get("passive_exit", True))

        unpaired = self.broker.unpaired_shares(m)
        if abs(unpaired) < MIN_TAKER_SHARES:
            self._over_since.pop(cid, None)
            await self._broker_call(self.broker.set_exit, m, None)
            return
        exposure = self.broker.net_yes_exposure_usd(m)
        h = hours_to_end(m, now)
        urgent = cid not in quoted or (h is not None and h <= exit_h)
        if not urgent:
            theme_markets = list(managed.values())
            if self.risk.theme_at_cap(m, theme_markets,
                                      self.broker.net_yes_exposure_usd,
                                      self._scale):
                urgent = True
        if not urgent:
            if abs(exposure) < threshold:
                self._over_since.pop(cid, None)
                await self._broker_call(self.broker.set_exit, m, None)
                return
            start = self._over_since.setdefault(cid, now)
            if passive:
                await self._update_exit_sell(m, unpaired)
            if now - start < wait:
                return
        if now - self._last_flatten.get(cid, 0.0) < FLATTEN_RETRY_SECONDS:
            return
        self._last_flatten[cid] = now
        excess_yes = unpaired > 0
        token = m.no_token if excess_yes else m.yes_token
        book = self.tracker.books.get(token)
        bid = book.best_bid if book else None
        ask = book.best_ask if book else None
        if ask is None or bid is None or ask - bid > max_spread:
            if passive:
                await self._update_exit_sell(m, unpaired)
            log.warning("forced hedge needed in '%s' but complement book is "
                        "wide/empty — retrying shortly", m.question[:45])
            return
        await self._broker_call(self.broker.set_exit, m, None)
        price = strategy._round_tick(min(ask, 1 - m.tick), m.tick)
        if self.paper:
            filled = self.broker.taker_buy(m, token, abs(unpaired), price)
        else:
            filled = await asyncio.to_thread(
                self.broker.taker_buy, m, token, abs(unpaired), price)
        if filled > 0:
            self._over_since.pop(cid, None)
            log.warning("FORCED HEDGE '%s': bought %.0f %s @ %.3f to pair off "
                        "$%.0f exposure", m.question[:45], filled,
                        "NO" if excess_yes else "YES", price, abs(exposure))

    async def _update_exit_sell(self, m: gamma.Market, unpaired: float) -> None:
        token = m.yes_token if unpaired > 0 else m.no_token
        book = self.tracker.books.get(token)
        mid = book.mid if book else None
        ask = book.best_ask if book else None
        size = float(int(abs(unpaired)))
        if mid is None or ask is None or size < MIN_TAKER_SHARES:
            await self._broker_call(self.broker.set_exit, m, None)
            return
        price = strategy._round_tick(max(ask, mid + m.tick), m.tick)
        price = min(price, strategy._round_tick(1.0 - m.tick, m.tick))
        cur = self.broker.exit_quote(m)
        move = self.cfg["quoting"]["requote_move_cents"]
        if (cur is not None and cur.token_id == token
                and abs(cur.price - price) * 100 < move
                and abs(cur.size - size) <= 0.1 * size):
            return
        await self._broker_call(self.broker.set_exit, m, strategy.Quote(token, price, size))

    def _token_mid(self, token_id: str) -> float | None:
        book = self.tracker.books.get(token_id)
        return book.mid if book else None

    def _fades(self, market: gamma.Market, now: float) -> tuple[float, float]:
        g = self.cfg["guards"]
        window = g["fade_window_minutes"] * 60
        per_fill = g["fade_cents_per_fill"] / 100.0
        cap = g["fade_max_cents"] / 100.0
        yes_n = no_n = 0
        for f in self.broker.fills_log:
            if f.get("taker") or f.get("exit"):
                continue
            if f.get("cid") == market.condition_id and now - f["ts"] <= window:
                if f["side"] == "YES":
                    yes_n += 1
                else:
                    no_n += 1
        return min(yes_n * per_fill, cap), min(no_n * per_fill, cap)

    def _sample_rewards(self) -> None:
        haircut = 1.0
        if self.paper:
            # The estimator only sees displayed competition and assumes full
            # eligibility; discount paper accrual until live data calibrates it.
            haircut = float((self.cfg.get("paper") or {}).get("reward_haircut", 0.7))
        for m in self.markets:
            share = strategy.estimate_reward_share(
                m,
                self.tracker.books[m.yes_token],
                self.tracker.books[m.no_token],
                self.broker.open_quotes(m),
            )
            self.broker.accrue_rewards(m.daily_pool * share * haircut / MINUTES_PER_DAY)

    def _print_status(self) -> None:
        table = Table(title=f"pmbot — {'PAPER' if self.paper else 'LIVE'}")
        for col in ("Market", "Mid", "Our bid YES", "Our bid NO", "Net exposure"):
            table.add_column(col)
        for m in self.markets:
            book = self.tracker.books[m.yes_token]
            quotes = {q.token_id: q for q in self.broker.open_quotes(m)}
            yq, nq = quotes.get(m.yes_token), quotes.get(m.no_token)
            table.add_row(
                m.question[:45],
                f"{book.mid:.3f}" if book.mid else "—",
                f"{yq.price:.3f} × {yq.size:.0f}" if yq else "—",
                f"{nq.price:.3f} × {nq.size:.0f}" if nq else "—",
                f"${self.broker.net_yes_exposure_usd(m):+.2f}",
            )
        console.print(table)
        stats = self.markouts.session_stats()
        if any(n for _, (_, n) in stats.items()):
            console.print("markouts: " + "  ".join(
                f"{avg:+.2f}c @{int(h)}s (n={n})"
                for h, (avg, n) in sorted(stats.items()) if n))
        uptime = self.metrics.session_uptime_pct()
        if uptime > 0:
            console.print(f"in-band uptime: {uptime:.1f}%")
        if self.controller.enabled:
            console.print(self.controller.status_line())
        eq = self.broker.equity()
        if eq != eq:
            return
        if self.paper:
            st = self.broker.state
            console.print(
                f"equity ${eq:.2f}  (cash ${st.cash:.2f}, est. rewards ${st.est_rewards:.4f}, "
                f"fills {sum(p.fills for p in st.positions.values())}, "
                f"PnL ${eq - st.start_equity:+.2f})"
            )
        else:
            console.print(
                f"equity ${eq:.2f}  (unpaired inventory ${self.broker.total_inventory_usd():.2f}, "
                f"day PnL ${eq - self.risk.day_start_equity:+.2f}, sizing ×{self._scale:.2f})"
            )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="pmbot")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("scan", "run", "report", "trades", "performance"):
        sub.add_parser(name)

    trades_p = sub.choices["trades"]
    trades_p.add_argument("--limit", type=int, default=50,
                          help="max fills to show (default 50)")
    trades_p.add_argument("--hours", type=float, default=None,
                          help="only fills from the last N hours")
    trades_p.add_argument("--csv", dest="export_csv", default=None,
                          help="export fills to CSV instead of printing")

    perf_p = sub.choices["performance"]
    perf_p.add_argument("--date", default=None,
                        help="UTC date YYYY-MM-DD (default: today)")

    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config(args.config)
    if args.command == "scan":
        cmd_scan(cfg)
    elif args.command == "report":
        cmd_report(cfg)
    elif args.command == "trades":
        cmd_trades(cfg, args.limit, args.hours, args.export_csv)
    elif args.command == "performance":
        cmd_performance(cfg, args.date)
    else:
        if cfg["mode"] == "live":
            console.print("[bold red]LIVE mode — real orders will be placed. Ctrl-C cancels all and exits.[/]")
        try:
            asyncio.run(Bot(cfg).run())
        except KeyboardInterrupt:
            console.print("stopped — all orders cancelled.")


if __name__ == "__main__":
    main()
