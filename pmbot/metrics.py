"""Structured metrics: SQLite logging, uptime tracking, PnL decomposition."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("pmbot.metrics")


class MetricsStore:
    def __init__(self, db_path: str = "data/metrics.db",
                 trades_log: str | None = None,
                 inception_date: str | None = None):
        self.path = Path(db_path)
        self.path.parent.mkdir(exist_ok=True)
        self._trades_log = Path(trades_log) if trades_log else None
        if self._trades_log:
            self._trades_log.parent.mkdir(exist_ok=True)
        # Reports reflect bot activity only: drop/refuse anything before this
        # UTC date (earlier rows were manual testing).
        self.inception_date = inception_date or None
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # Tolerate brief contention from a concurrent reader/backfill instead of
        # raising "database is locked" immediately.
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Order ops run concurrently in worker threads and all record metrics
        # through this single connection — serialize writes.
        self._lock = threading.Lock()
        self._init_schema()
        self._prune_before_inception()
        self._uptime_samples: dict[str, list[bool]] = {}
        self._last_uptime_minute: int = 0
        self._session_start = time.time()

    def _inception_ts(self) -> float | None:
        if not self.inception_date:
            return None
        try:
            return datetime.strptime(self.inception_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            log.warning("invalid metrics.inception_date %r; ignoring",
                        self.inception_date)
            return None

    def _prune_before_inception(self) -> None:
        """Delete rows that predate the inception date (manual-testing data)."""
        ts = self._inception_ts()
        if ts is None:
            return
        with self._lock:
            for tbl in ("fills", "hedges", "merges", "equity", "markouts",
                        "quotes"):
                self._conn.execute(f"DELETE FROM {tbl} WHERE ts < ?", (ts,))
            self._conn.execute("DELETE FROM uptime WHERE minute_ts < ?",
                               (int(ts) // 60,))
            self._conn.execute("DELETE FROM reward_samples WHERE minute_ts < ?",
                               (int(ts) // 60,))
            self._conn.execute("DELETE FROM rewards WHERE date < ?",
                               (self.inception_date,))
            self._conn.commit()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, market TEXT, side TEXT,
                token TEXT, price REAL, size REAL,
                taker INTEGER DEFAULT 0, exit INTEGER DEFAULT 0, merged REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, quotes_json TEXT
            );
            CREATE TABLE IF NOT EXISTS hedges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, price REAL, size REAL
            );
            CREATE TABLE IF NOT EXISTS merges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, cid TEXT, pairs REAL
            );
            CREATE TABLE IF NOT EXISTS equity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, equity REAL, inventory_usd REAL
            );
            CREATE TABLE IF NOT EXISTS uptime (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                minute_ts INTEGER, cid TEXT, in_band INTEGER
            );
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, date TEXT, estimated REAL DEFAULT 0,
                realized REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reward_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                minute_ts INTEGER, cid TEXT, est_usd REAL
            );
            CREATE INDEX IF NOT EXISTS idx_reward_samples_minute
                ON reward_samples (minute_ts);
            CREATE TABLE IF NOT EXISTS markouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, fill_ts REAL, cid TEXT, market TEXT,
                horizon REAL, markout REAL
            );
        """)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(fills)")}
        if "fee" not in cols:
            self._conn.execute("ALTER TABLE fills ADD COLUMN fee REAL DEFAULT 0")
        self._conn.commit()

    def _append_trades_log(self, entry: dict) -> None:
        if self._trades_log is None:
            return
        try:
            with self._trades_log.open("a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as e:
            log.warning("could not append trades log: %s", e)

    def record_fill(self, entry: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO fills (ts,cid,market,side,token,price,size,taker,exit,merged,fee) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (entry.get("ts", time.time()), entry.get("cid"), entry.get("market"),
                 entry.get("side"), entry.get("token"), entry.get("price", 0),
                 entry.get("size", 0), int(bool(entry.get("taker"))),
                 int(bool(entry.get("exit"))), entry.get("merged", 0),
                 entry.get("fee", 0)),
            )
            self._conn.commit()
        self._append_trades_log(entry)

    def record_markout(self, entry: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO markouts (ts,fill_ts,cid,market,horizon,markout) "
                "VALUES (?,?,?,?,?,?)",
                (entry.get("ts", time.time()), entry.get("fill_ts"),
                 entry.get("cid"), entry.get("market"),
                 entry.get("horizon"), entry.get("markout")),
            )
            self._conn.commit()

    def record_quotes(self, cid: str, quotes: list) -> None:
        data = [{"token": q.token_id, "price": q.price, "size": q.size} for q in quotes]
        with self._lock:
            self._conn.execute(
                "INSERT INTO quotes (ts, cid, quotes_json) VALUES (?,?,?)",
                (time.time(), cid, json.dumps(data)),
            )
            self._conn.commit()

    def record_hedge(self, cid: str, price: float, size: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO hedges (ts, cid, price, size) VALUES (?,?,?,?)",
                (time.time(), cid, price, size),
            )
            self._conn.commit()

    def record_merge(self, cid: str, pairs: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO merges (ts, cid, pairs) VALUES (?,?,?)",
                (time.time(), cid, pairs),
            )
            self._conn.commit()

    def record_equity(self, equity: float, inventory_usd: float) -> None:
        if equity != equity:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity (ts, equity, inventory_usd) VALUES (?,?,?)",
                (time.time(), equity, inventory_usd),
            )
            self._conn.commit()

    def record_est_reward(self, usd: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                "SELECT id, estimated FROM rewards WHERE date=? ORDER BY id DESC LIMIT 1",
                (today,),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE rewards SET estimated=estimated+? WHERE id=?",
                    (usd, row[0]),
                )
            else:
                self._conn.execute(
                    "INSERT INTO rewards (ts, date, estimated) VALUES (?,?,?)",
                    (time.time(), today, usd),
                )
            self._conn.commit()

    def record_reward_sample(self, cid: str, usd: float) -> None:
        """Per-minute, per-market estimated reward accrual (USD for that minute).

        The daily ``rewards`` table only keeps a running daily total, which
        makes the reward *rate* invisible — diagnosing intra-session decay
        meant inferring it from uptime and churn. This table keeps the raw
        time series (one row per market per ~minute) so the rate is directly
        observable and changes can be proven rather than inferred.
        """
        if usd != usd:  # NaN guard
            return
        minute = int(time.time()) // 60
        with self._lock:
            self._conn.execute(
                "INSERT INTO reward_samples (minute_ts, cid, est_usd) "
                "VALUES (?,?,?)",
                (minute, cid, usd),
            )
            self._conn.commit()

    def reward_rate_recent(self, minutes: int = 60) -> dict:
        """Estimated reward accrual over the last ``minutes``, as a rate.

        Each sample is the USD accrued in its minute, so summing the window
        gives the dollars accrued in it. ``usd_per_hr`` divides that by the
        number of *distinct sampled minutes* (not wall-clock), so a bot that
        only ran part of the window isn't penalised for the idle stretch.
        """
        cutoff = int(time.time()) // 60 - int(minutes)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(est_usd),0), COUNT(DISTINCT minute_ts) "
            "FROM reward_samples WHERE minute_ts > ?",
            (cutoff,),
        ).fetchone()
        usd, n = (row[0] or 0.0), (row[1] or 0)
        usd_per_hr = (usd / (n / 60.0)) if n else 0.0
        return {"usd": usd, "minutes": n, "usd_per_hr": usd_per_hr}

    def reward_rate_by_market(self, since_minute: int) -> dict[str, dict]:
        """Per-market estimated accrual since ``since_minute`` (a minute_ts).

        Returns ``{cid: {"usd": total, "minutes": n, "usd_per_hr": rate}}`` so
        you can see which held markets are actually carrying the reward rate.
        """
        rows = self._conn.execute(
            "SELECT cid, COALESCE(SUM(est_usd),0), COUNT(DISTINCT minute_ts) "
            "FROM reward_samples WHERE minute_ts >= ? GROUP BY cid",
            (int(since_minute),),
        ).fetchall()
        out: dict[str, dict] = {}
        for cid, usd, n in rows:
            out[cid] = {
                "usd": usd or 0.0,
                "minutes": n or 0,
                "usd_per_hr": ((usd or 0.0) / (n / 60.0)) if n else 0.0,
            }
        return out

    def record_realized_reward(self, date: str, usd: float) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM rewards WHERE date=? ORDER BY id DESC LIMIT 1", (date,),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE rewards SET realized=? WHERE id=?", (usd, row[0]),
                )
            else:
                self._conn.execute(
                    "INSERT INTO rewards (ts, date, realized) VALUES (?,?,?)",
                    (time.time(), date, usd),
                )
            self._conn.commit()

    def sample_uptime(self, cid: str, in_band: bool) -> None:
        self._uptime_samples.setdefault(cid, []).append(in_band)
        minute = int(time.time()) // 60
        if minute != self._last_uptime_minute:
            self._flush_uptime(minute)
            self._last_uptime_minute = minute

    def _flush_uptime(self, minute: int) -> None:
        with self._lock:
            for cid, samples in self._uptime_samples.items():
                if not samples:
                    continue
                in_band = sum(samples) / len(samples) >= 0.5
                self._conn.execute(
                    "INSERT INTO uptime (minute_ts, cid, in_band) VALUES (?,?,?)",
                    (minute, cid, int(in_band)),
                )
                self._uptime_samples[cid] = []
            self._conn.commit()

    def session_uptime_pct(self) -> float:
        rows = self._conn.execute(
            "SELECT in_band FROM uptime WHERE minute_ts >= ?",
            (int(self._session_start) // 60,),
        ).fetchall()
        if not rows:
            return 0.0
        return sum(r[0] for r in rows) / len(rows) * 100

    def uptime_pct_by_market(self, since_minute: int,
                             min_samples: int = 10) -> dict[str, float]:
        """In-band uptime % per market over the recent window.

        Used by sticky market selection to decide whether a held market is
        actually farming rewards (high in-band %) or underperforming. Markets
        with fewer than ``min_samples`` minutes of history are omitted, so a
        freshly-entered market is treated as 'performing' (protected) until it
        has a real track record rather than being evicted on thin data.
        """
        rows = self._conn.execute(
            "SELECT cid, AVG(in_band) * 100.0, COUNT(*) FROM uptime "
            "WHERE minute_ts >= ? GROUP BY cid",
            (int(since_minute),),
        ).fetchall()
        return {cid: pct for cid, pct, n in rows if n >= min_samples}

    @staticmethod
    def _sum_earnings(rows) -> float:
        """USD realized rewards from a /rewards/user[/total] payload.

        The CLOB returns one row per collateral asset:
        {date, asset_address, maker_address, earnings, asset_rate}. `earnings`
        is denominated in the asset (pUSD/USDC) and `asset_rate` is its USD
        price (~1.0), so USD = sum(earnings * asset_rate). Some deployments wrap
        the list in {"data": [...]}; handle both.
        """
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("earnings") or []
        total = 0.0
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            earnings = float(item.get("earnings") or item.get("amount")
                             or item.get("reward") or 0.0)
            rate = float(item.get("asset_rate") or 1.0)
            total += earnings * rate
        return total

    def fetch_realized_rewards(self, client, date: str | None = None) -> float:
        """Realized liquidity rewards (USD) for a UTC day, from the CLOB.

        Uses the authenticated `/rewards/user/total` endpoint via the official
        client method, which signs the request correctly (L2 HMAC over the bare
        path), passes the wallet `signature_type`, and handles pagination — the
        previous hand-rolled call signed the path WITH its query string and
        omitted signature_type, so it 401'd and silently recorded $0.

        Records the day's realized total so report/backtest can compare it to
        the estimate. Best-effort: on a transient fetch error we leave any
        previously recorded value intact (return without overwriting).
        """
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.inception_date and date < self.inception_date:
            return 0.0
        try:
            rows = client.get_total_earnings_for_user_for_day(date)
        except Exception as e:  # noqa: BLE001
            log.debug("realized rewards fetch failed for %s: %s", date, e)
            return 0.0
        total = self._sum_earnings(rows)
        self.record_realized_reward(date, total)
        return total

    def backfill_realized_rewards(self, client, days: int = 7) -> dict[str, float]:
        """Re-fetch and record realized rewards for the last `days` UTC dates.

        Lets recently-finalized days (rewards post shortly after UTC midnight)
        and any rows left at $0 by the old bug self-heal. Returns {date: usd}.
        """
        out: dict[str, float] = {}
        today = datetime.now(timezone.utc).date()
        for d in range(days):
            date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
            if self.inception_date and date < self.inception_date:
                continue
            out[date] = self.fetch_realized_rewards(client, date)
        return out

    def daily_report(self, date: str | None = None) -> dict:
        """PnL decomposition for a UTC day."""
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_start = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
        day_end = day_start + 86400

        merges = self._conn.execute(
            "SELECT COALESCE(SUM(pairs),0) FROM merges WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ).fetchone()[0]

        hedge_cost = self._conn.execute(
            "SELECT COALESCE(SUM(price*size),0) FROM hedges WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ).fetchone()[0]

        fees = self._conn.execute(
            "SELECT COALESCE(SUM(fee),0) FROM fills WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ).fetchone()[0]

        # Net trading P&L from the cash ledger (merges + sells - buys - fees) —
        # the ground truth, vs the gross component figures above.
        buys = self._conn.execute(
            "SELECT COALESCE(SUM(price*size),0) FROM fills "
            "WHERE ts>=? AND ts<? AND exit=0",
            (day_start, day_end),
        ).fetchone()[0]
        sells = self._conn.execute(
            "SELECT COALESCE(SUM(price*size),0) FROM fills "
            "WHERE ts>=? AND ts<? AND exit=1",
            (day_start, day_end),
        ).fetchone()[0]
        trading_pnl = merges + sells - buys - fees

        est_rewards = self._conn.execute(
            "SELECT COALESCE(SUM(estimated),0) FROM rewards WHERE date=?",
            (date,),
        ).fetchone()[0]

        realized_rewards = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM rewards WHERE date=?",
            (date,),
        ).fetchone()[0]

        fill_count = self._conn.execute(
            "SELECT COUNT(*) FROM fills WHERE ts>=? AND ts<? AND taker=0 AND exit=0",
            (day_start, day_end),
        ).fetchone()[0]

        equity_rows = self._conn.execute(
            "SELECT equity FROM equity WHERE ts>=? AND ts<? ORDER BY ts",
            (day_start, day_end),
        ).fetchall()
        equity_pnl = 0.0
        if len(equity_rows) >= 2:
            equity_pnl = equity_rows[-1][0] - equity_rows[0][0]

        uptime_rows = self._conn.execute(
            "SELECT in_band FROM uptime WHERE minute_ts>=? AND minute_ts<?",
            (int(day_start) // 60, int(day_end) // 60),
        ).fetchall()
        uptime_pct = (sum(r[0] for r in uptime_rows) / len(uptime_rows) * 100
                      if uptime_rows else 0.0)

        return {
            "date": date,
            "merge_proceeds_usd": merges,
            "buys_usd": buys,
            "sells_usd": sells,
            "trading_pnl_usd": trading_pnl,
            "spread_capture_usd": merges,
            "hedge_cost_usd": hedge_cost,
            "fees_usd": -fees,
            "est_rewards_usd": est_rewards,
            "realized_rewards_usd": realized_rewards,
            "equity_pnl_usd": equity_pnl,
            "maker_fills": fill_count,
            "uptime_pct": uptime_pct,
        }

    def reward_totals(self) -> dict:
        """Realized/estimated rewards, all-time and rolling last 24h.

        Realized rewards are stored per UTC day (the CLOB finalizes them
        daily), so the 24h figure is the sum over days whose record timestamp
        falls in the last 24h — in practice today's (and possibly yesterday's
        just-finalized) total.
        """
        realized_total = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM rewards").fetchone()[0]
        est_total = self._conn.execute(
            "SELECT COALESCE(SUM(estimated),0) FROM rewards").fetchone()[0]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        realized_24h = self._conn.execute(
            "SELECT COALESCE(SUM(realized),0) FROM rewards WHERE date=?",
            (today,)).fetchone()[0]
        est_24h = self._conn.execute(
            "SELECT COALESCE(SUM(estimated),0) FROM rewards WHERE date=?",
            (today,)).fetchone()[0]
        return {
            "realized_total": realized_total, "realized_24h": realized_24h,
            "est_total": est_total, "est_24h": est_24h,
        }

    def hedge_pnl_totals(self) -> dict:
        """Estimated P&L of forced hedges, all-time and rolling last 24h.

        A forced hedge taker-buys the complement of a maker leg we already hold;
        the completed pair then merges back to $1. The realized P&L on a
        completed pair is

            (1 - hedge_price - basis)

        where `basis` is our cost for the held leg, approximated by the average
        maker fill price of the *majority* maker side in that market (the side
        we accumulate is the one that needs hedging). Crucially we cap the
        number of loss-bearing pairs at the maker shares we actually held —
        hedge shares beyond that were open taker buys that merged against other
        flow or resolved, and aren't a realized pairing loss. Hedges in markets
        with no recorded maker leg are skipped (no basis to estimate from).

        This is an ESTIMATE — lot-level pairing isn't logged, so treat it as
        indicative, not audited. Realized rewards are exact; for the true
        bottom line compare deposits vs wallet balance. `spend_*` is the gross
        taker dollars for reference.
        """
        maker: dict[str, dict[str, tuple[float, float]]] = {}
        for cid, side, notional, shares in self._conn.execute(
            "SELECT cid, side, COALESCE(SUM(price*size),0), COALESCE(SUM(size),0) "
            "FROM fills WHERE taker=0 AND exit=0 GROUP BY cid, side"
        ):
            avg = (notional / shares) if shares else 0.0
            maker.setdefault(cid, {})[side] = (avg, shares)

        cutoff = time.time() - 86400
        agg: dict[str, dict[str, float]] = {}
        for cid, ts, price, size in self._conn.execute(
            "SELECT cid, ts, price, size FROM hedges"
        ):
            a = agg.setdefault(cid, {"pv": 0.0, "sz": 0.0,
                                     "pv24": 0.0, "sz24": 0.0})
            a["pv"] += price * size
            a["sz"] += size
            if ts >= cutoff:
                a["pv24"] += price * size
                a["sz24"] += size

        out = {"pnl_total": 0.0, "pnl_24h": 0.0, "spend_total": 0.0,
               "spend_24h": 0.0, "shares_total": 0.0, "shares_24h": 0.0}
        for cid, a in agg.items():
            sides = maker.get(cid, {})
            y_px, y_sz = sides.get("YES", (0.0, 0.0))
            n_px, n_sz = sides.get("NO", (0.0, 0.0))
            # The leg we hold (and must hedge) is the majority maker side.
            basis, held = (y_px, y_sz) if y_sz >= n_sz else (n_px, n_sz)
            out["spend_total"] += a["pv"]
            out["shares_total"] += a["sz"]
            out["spend_24h"] += a["pv24"]
            out["shares_24h"] += a["sz24"]
            if held <= 0:
                continue
            if a["sz"] > 0:
                hpx = a["pv"] / a["sz"]
                out["pnl_total"] += min(a["sz"], held) * (1.0 - hpx - basis)
            if a["sz24"] > 0:
                hpx24 = a["pv24"] / a["sz24"]
                out["pnl_24h"] += min(a["sz24"], held) * (1.0 - hpx24 - basis)
        return out

    def trading_pnl_ledger(self) -> dict:
        """Ground-truth realized trading P&L from the cash ledger.

        Reconciles the ACTUAL logged cashflows the way the Polymarket trade
        history does, rather than estimating forced-hedge pairing loss like
        ``hedge_pnl_totals`` (which assumes a basis and caps loss-bearing
        pairs, and was found to understate the real loss ~2x):

            realized = merges($1/pair) + exits(sells) - buys - fees

        Every fill with ``exit=0`` is cash OUT (maker reward quotes AND taker
        forced hedges); every ``exit=1`` fill is cash IN; each merged pair
        returns $1. Rewards and deposits are EXCLUDED — they are not trading
        P&L. ``mtm_total`` adds the latest inventory mark so open (bought but
        not-yet-merged) pairs aren't counted as pure loss; ``realized_*`` treat
        held inventory as sunk, so short windows read low until those pairs
        merge/resolve. This is the apples-to-apples match for your Polymarket
        history (sum of +/- trades - deposits - rewards).

        Caveat: positions that resolved and redeemed at $1 without a logged
        merge are not captured here, which would make the ledger look slightly
        worse than reality; with ``merge_enabled`` this should be small.
        """
        def _cash(since: float | None) -> float:
            extra = "" if since is None else " AND ts >= ?"
            args = () if since is None else (since,)
            margs = () if since is None else (since,)
            buys = self._conn.execute(
                "SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=0" + extra,
                args).fetchone()[0] or 0.0
            sells = self._conn.execute(
                "SELECT COALESCE(SUM(price*size),0) FROM fills WHERE exit=1" + extra,
                args).fetchone()[0] or 0.0
            fees = self._conn.execute(
                "SELECT COALESCE(SUM(fee),0) FROM fills"
                + ("" if since is None else " WHERE ts >= ?"), margs).fetchone()[0] or 0.0
            merge_cash = self._conn.execute(
                "SELECT COALESCE(SUM(pairs),0) FROM merges"
                + ("" if since is None else " WHERE ts >= ?"), margs).fetchone()[0] or 0.0
            return merge_cash + sells - buys - fees

        inv_row = self._conn.execute(
            "SELECT inventory_usd FROM equity ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        inv = float(inv_row[0]) if inv_row and inv_row[0] is not None else 0.0
        realized_total = _cash(None)
        realized_24h = _cash(time.time() - 86400)
        return {
            "realized_total": realized_total,
            "realized_24h": realized_24h,
            "inventory_usd": inv,
            "mtm_total": realized_total + inv,
        }

    def recent_fills(self, limit: int = 50, since_ts: float | None = None,
                     cid: str | None = None) -> list[dict]:
        """Return recent fills newest-first."""
        clauses, params = [], []
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if cid:
            clauses.append("cid = ?")
            params.append(cid)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT ts,cid,market,side,token,price,size,taker,exit,merged,fee "
            f"FROM fills {where} ORDER BY ts DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "ts": r[0], "cid": r[1], "market": r[2], "side": r[3],
                "token": r[4], "price": r[5], "size": r[6],
                "taker": bool(r[7]), "exit": bool(r[8]),
                "merged": r[9], "fee": r[10],
            }
            for r in rows
        ]

    def performance_report(self, date: str | None = None) -> dict:
        """Per-market breakdown for tuning: fills, merges, hedges, markouts, uptime."""
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_start = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
        day_end = day_start + 86400
        minute_start = int(day_start) // 60
        minute_end = int(day_end) // 60

        markets: dict[str, dict] = {}

        def _ensure(cid: str, name: str = "") -> dict:
            if cid not in markets:
                markets[cid] = {
                    "cid": cid,
                    "market": name,
                    "maker_fills": 0,
                    "taker_fills": 0,
                    "exits": 0,
                    "merged_pairs": 0.0,
                    "hedge_cost_usd": 0.0,
                    "fees_usd": 0.0,
                    "markout_cents": None,
                    "markout_n": 0,
                    "uptime_pct": 0.0,
                }
            elif name and not markets[cid]["market"]:
                markets[cid]["market"] = name
            return markets[cid]

        for row in self._conn.execute(
            "SELECT cid, market, taker, exit, merged, fee FROM fills "
            "WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ):
            cid, name, taker, exit_, merged, fee = row
            m = _ensure(cid, name or "")
            if exit_:
                m["exits"] += 1
            elif taker:
                m["taker_fills"] += 1
            else:
                m["maker_fills"] += 1
            m["merged_pairs"] += merged or 0
            m["fees_usd"] += fee or 0

        for cid, cost in self._conn.execute(
            "SELECT cid, COALESCE(SUM(price*size),0) FROM hedges "
            "WHERE ts>=? AND ts<? GROUP BY cid",
            (day_start, day_end),
        ):
            _ensure(cid)["hedge_cost_usd"] = cost

        markout_rows = self._conn.execute(
            "SELECT cid, market, markout, horizon FROM markouts "
            "WHERE ts>=? AND ts<?",
            (day_start, day_end),
        ).fetchall()
        if markout_rows:
            max_h = max(r[3] for r in markout_rows)
            by_cid: dict[str, list[float]] = {}
            for cid, name, markout, horizon in markout_rows:
                if horizon != max_h:
                    continue
                _ensure(cid, name or "")
                by_cid.setdefault(cid, []).append(markout)
            for cid, vals in by_cid.items():
                m = markets[cid]
                m["markout_n"] = len(vals)
                m["markout_cents"] = sum(vals) / len(vals) * 100

        for cid, total, in_band in self._conn.execute(
            "SELECT cid, COUNT(*), COALESCE(SUM(in_band),0) FROM uptime "
            "WHERE minute_ts>=? AND minute_ts<? GROUP BY cid",
            (minute_start, minute_end),
        ):
            m = _ensure(cid)
            m["uptime_pct"] = (in_band / total * 100) if total else 0.0

        rows = sorted(
            markets.values(),
            key=lambda r: (
                r["maker_fills"] + r["taker_fills"] + r["exits"],
                r["merged_pairs"],
            ),
            reverse=True,
        )
        summary = self.daily_report(date)
        return {"date": date, "summary": summary, "markets": rows}

    def close(self) -> None:
        self._flush_uptime(int(time.time()) // 60)
        self._conn.close()
