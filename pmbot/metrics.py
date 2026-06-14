"""Structured metrics: SQLite logging, uptime tracking, PnL decomposition."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("pmbot.metrics")

CLOB_URL = "https://clob.polymarket.com"


class MetricsStore:
    def __init__(self, db_path: str = "data/metrics.db",
                 trades_log: str | None = None):
        self.path = Path(db_path)
        self.path.parent.mkdir(exist_ok=True)
        self._trades_log = Path(trades_log) if trades_log else None
        if self._trades_log:
            self._trades_log.parent.mkdir(exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # Order ops run concurrently in worker threads and all record metrics
        # through this single connection — serialize writes.
        self._lock = threading.Lock()
        self._init_schema()
        self._uptime_samples: dict[str, list[bool]] = {}
        self._last_uptime_minute: int = 0
        self._session_start = time.time()

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

    def fetch_realized_rewards(self, client, date: str | None = None) -> float:
        """Best-effort fetch of realized rewards from CLOB API."""
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            from py_clob_client_v2.headers.headers import create_level_2_headers
            from py_clob_client_v2.clob_types import RequestArgs

            path = f"/rewards/user?date={date}"
            request_args = RequestArgs(method="GET", request_path=path)
            headers = create_level_2_headers(client.signer, client.creds, request_args)
            resp = httpx.get(f"{CLOB_URL}{path}", headers=headers, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            total = 0.0
            if isinstance(data, list):
                for item in data:
                    total += float(item.get("amount") or item.get("reward") or 0)
            elif isinstance(data, dict):
                total = float(data.get("total") or data.get("amount") or 0)
            if total > 0:
                self.record_realized_reward(date, total)
            return total
        except Exception as e:  # noqa: BLE001
            log.debug("realized rewards fetch failed: %s", e)
            return 0.0

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
            "spread_capture_usd": merges,
            "hedge_cost_usd": hedge_cost,
            "fees_usd": -fees,
            "est_rewards_usd": est_rewards,
            "realized_rewards_usd": realized_rewards,
            "equity_pnl_usd": equity_pnl,
            "maker_fills": fill_count,
            "uptime_pct": uptime_pct,
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
