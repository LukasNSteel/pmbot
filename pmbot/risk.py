"""Risk manager: daily loss limit, hard kill, inventory caps, toxicity guards."""

from __future__ import annotations

import logging
import statistics
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

log = logging.getLogger("pmbot.risk")


class RiskAction(Enum):
    OK = "ok"
    PAUSE_QUOTES = "pause_quotes"  # cancel quotes until risk state is healthy
    PAUSE_DAY = "pause_day"  # cancel quotes, resume next UTC day
    KILL = "kill"  # cancel everything and exit


class RiskManager:
    def __init__(self, cfg: dict, start_equity: float):
        self.cfg = cfg["risk"]
        self.baseline_capital = float(cfg["capital_usd"])
        self.day_start_equity = start_equity
        self.day = self._today()
        self.paused = False
        self._equity_history: deque[float] = deque(maxlen=20)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def scale(self, equity: float) -> float:
        if not self.cfg.get("scale_with_equity") or equity != equity:
            return 1.0
        raw = equity / max(self.baseline_capital, 1e-9)
        return max(float(self.cfg["scale_min"]), min(float(self.cfg["scale_max"]), raw))

    def _smoothed_equity(self, equity: float) -> float:
        """Median of recent equity readings for loss-limit checks."""
        self._equity_history.append(equity)
        if len(self._equity_history) < 3:
            return equity
        return statistics.median(self._equity_history)

    def check(self, equity: float, total_inventory_usd: float,
              scale: float = 1.0) -> RiskAction:
        if equity != equity:
            log.warning("equity unknown — pausing quotes until balance/positions refresh")
            return RiskAction.PAUSE_QUOTES
        if self.day_start_equity != self.day_start_equity:
            self.day_start_equity = equity
            log.info("equity baseline set: $%.2f", equity)

        today = self._today()
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
            self._equity_history.clear()
            if self.paused:
                log.info("new UTC day — resuming after pause")
                self.paused = False

        smoothed = self._smoothed_equity(equity)
        day_loss = self.day_start_equity - smoothed
        if day_loss >= self.cfg["hard_kill_loss_usd"]:
            log.error("HARD KILL: daily loss $%.2f >= $%.2f",
                      day_loss, self.cfg["hard_kill_loss_usd"])
            return RiskAction.KILL
        if day_loss >= self.cfg["daily_loss_limit_usd"]:
            if not self.paused:
                log.warning("daily loss limit hit ($%.2f) — pausing until next UTC day", day_loss)
                self.paused = True
            return RiskAction.PAUSE_DAY
        if self.paused:
            return RiskAction.PAUSE_DAY

        if total_inventory_usd > self.cfg["max_total_inventory_usd"] * scale:
            log.warning("total inventory $%.0f over cap — pausing new quotes",
                        total_inventory_usd)
            return RiskAction.PAUSE_QUOTES
        return RiskAction.OK

    def market_inventory_ok(self, net_exposure_usd: float, cap: float | None = None) -> bool:
        if cap is None:
            cap = self.cfg["max_inventory_usd_per_market"]
        return abs(net_exposure_usd) < cap

    def _theme_groups(self) -> dict[str, list[str]]:
        raw = self.cfg.get("theme_groups") or {}
        return {name: [k.lower() for k in keywords] for name, keywords in raw.items()}

    def market_themes(self, market) -> list[str]:
        """Theme names for keyword groups and neg-risk event groups."""
        themes = []
        q = market.question.lower()
        for name, keywords in self._theme_groups().items():
            if any(k in q for k in keywords):
                themes.append(name)
        event_id = getattr(market, "event_id", None)
        if event_id:
            themes.append(f"event:{event_id}")
        return themes

    def theme_exposure_usd(self, theme: str, markets, net_exposure_fn) -> float:
        return sum(
            abs(net_exposure_fn(m))
            for m in markets
            if theme in self.market_themes(m)
        )

    def _theme_cap(self, scale: float) -> float:
        cap = float(self.cfg.get("theme_max_inventory_usd") or 0)
        return cap * scale if cap > 0 else 0.0

    def theme_quoting_ok(self, market, markets, net_exposure_fn, scale: float = 1.0) -> bool:
        cap = self._theme_cap(scale)
        if cap <= 0:
            return True
        if abs(net_exposure_fn(market)) >= 0.01:
            return True
        return all(
            self.theme_exposure_usd(t, markets, net_exposure_fn) < cap
            for t in self.market_themes(market)
        )

    def theme_at_cap(self, market, markets, net_exposure_fn, scale: float = 1.0) -> bool:
        cap = self._theme_cap(scale)
        if cap <= 0:
            return False
        return any(
            self.theme_exposure_usd(t, markets, net_exposure_fn) >= cap
            for t in self.market_themes(market)
        )


class MarketGuards:
    """Per-market circuit breakers against adverse selection.

    Optional callbacks fire when a guard newly trips, so the bot can pull
    quotes immediately instead of waiting for the next loop tick:
      on_trip(condition_id)    — whole-market pause
      on_side_block(token_id)  — single-side pull
    """

    def __init__(self, cfg: dict):
        self.on_trip: Callable[[str], None] | None = None
        self.on_side_block: Callable[[str], None] | None = None
        self._load(cfg)
        self._mids: dict[str, list[tuple[float, float]]] = {}
        self._paused_until: dict[str, float] = {}
        self._trade_times: dict[str, deque] = {}
        self._taker_sides: dict[str, deque] = {}
        self._side_blocked_until: dict[str, float] = {}
        self._flow: dict[str, deque] = {}

    def _load(self, cfg: dict) -> None:
        g = cfg["guards"]
        self.vol_window = float(g["vol_window_secs"])
        self.vol_move = float(g["vol_max_move_cents"]) / 100.0
        self.max_fills = int(g["max_same_side_fills"])
        self.fill_window = float(g["same_side_window_minutes"]) * 60
        self.cooldown = float(g["market_cooldown_minutes"]) * 60
        self.vel_window = float(g["velocity_window_secs"])
        self.vel_max = int(g["velocity_max_trades"])
        self.dir_consec = int(g["directional_consecutive"])
        self.side_cooldown = float(g["side_cooldown_minutes"]) * 60
        self.flow_window = float(g["flow_window_secs"])
        self.flow_min_vol = float(g["flow_min_volume_shares"])
        self.flow_widen_thr = float(g["flow_widen_threshold"])
        self.flow_pull_thr = float(g["flow_pull_threshold"])
        self.flow_widen_max = float(g["flow_widen_max_cents"]) / 100.0

    def reload(self, cfg: dict) -> None:
        """Re-read guard thresholds after the controller mutates config.

        Only refreshes scalar thresholds; in-flight per-market state
        (cooldowns, flow deques, taker-side history) is preserved.
        """
        self._load(cfg)

    def allow(self, cid: str, now: float) -> bool:
        return now >= self._paused_until.get(cid, 0.0)

    def paused_cids(self, now: float) -> set[str]:
        """Markets currently inside their trip cooldown (not quotable now)."""
        return {cid for cid, until in self._paused_until.items() if now < until}

    def _trip(self, cid: str, now: float, reason: str, question: str) -> None:
        newly_tripped = self.allow(cid, now)
        if newly_tripped:
            log.warning("guard tripped (%s) — pausing '%s' for %.0f min",
                        reason, question[:50], self.cooldown / 60)
        self._paused_until[cid] = now + self.cooldown
        if newly_tripped and self.on_trip is not None:
            self.on_trip(cid)

    def record_mid(self, cid: str, mid: float, now: float, question: str) -> None:
        hist = self._mids.setdefault(cid, [])
        hist.append((now, mid))
        cutoff = now - self.vol_window
        while hist and hist[0][0] < cutoff:
            hist.pop(0)
        if hist and abs(mid - hist[0][1]) >= self.vol_move:
            self._trip(cid, now, f"mid moved {abs(mid - hist[0][1]) * 100:.1f}c "
                                 f"in {now - hist[0][0]:.0f}s", question)

    def allow_side(self, token_id: str, now: float) -> bool:
        return now >= self._side_blocked_until.get(token_id, 0.0)

    def trip_market(self, cid: str, now: float, reason: str, question: str) -> None:
        self._trip(cid, now, reason, question)

    def record_trade(self, market, token_id: str, side: str, size: float,
                     now: float) -> None:
        cid = market.condition_id
        if size > 0 and side.upper() in ("BUY", "SELL"):
            sign = 1 if (side.upper() == "BUY") == (token_id == market.yes_token) else -1
            flow = self._flow.setdefault(cid, deque())
            flow.append((now, sign * size))
        times = self._trade_times.setdefault(cid, deque())
        times.append(now)
        cutoff = now - self.vel_window
        while times and times[0] < cutoff:
            times.popleft()
        if len(times) >= self.vel_max:
            self._trip(cid, now, f"{len(times)} trades in {self.vel_window:.0f}s",
                       market.question)
            times.clear()
            return

        s = side.upper()
        if s not in ("BUY", "SELL"):
            return
        sides = self._taker_sides.setdefault(token_id, deque(maxlen=self.dir_consec))
        sides.append(s)
        if len(sides) == self.dir_consec and len(set(sides)) == 1:
            if s == "SELL":
                blocked = token_id
            else:
                blocked = market.no_token if token_id == market.yes_token else market.yes_token
            newly_blocked = self.allow_side(blocked, now)
            if newly_blocked:
                log.warning("directional flow (%d × %s) — pulling one bid in '%s' for %.0f min",
                            self.dir_consec, s, market.question[:45], self.side_cooldown / 60)
            self._side_blocked_until[blocked] = now + self.side_cooldown
            if newly_blocked and self.on_side_block is not None:
                self.on_side_block(blocked)
            sides.clear()

    def _flow_stats(self, market, now: float) -> tuple[float, float, float]:
        """Return (volume, net_signed, imbalance_ratio) in YES-equivalent terms."""
        flow = self._flow.get(market.condition_id)
        if not flow:
            return 0.0, 0.0, 0.0
        cutoff = now - self.flow_window
        while flow and flow[0][0] < cutoff:
            flow.popleft()
        volume = sum(abs(s) for _, s in flow)
        if volume < self.flow_min_vol:
            return volume, 0.0, 0.0
        net = sum(s for _, s in flow)
        return volume, net, abs(net) / volume

    def flow_imbalance(self, market, now: float) -> float:
        """Signed flow imbalance in [-1, 1] for strategy drift."""
        _, net, imb = self._flow_stats(market, now)
        if imb < self.flow_widen_thr:
            return 0.0
        sign = 1.0 if net > 0 else -1.0
        return sign * imb

    def check_flow(self, market, now: float) -> tuple[float, float]:
        """Returns (widen_yes, widen_no) in price units for the endangered side."""
        volume, net, imbalance = self._flow_stats(market, now)
        if volume < self.flow_min_vol or imbalance < self.flow_widen_thr:
            return 0.0, 0.0
        endangered_no = net > 0
        if imbalance >= self.flow_pull_thr:
            blocked = market.no_token if endangered_no else market.yes_token
            newly_blocked = self.allow_side(blocked, now)
            if newly_blocked:
                log.warning("flow imbalance %.0f%% on %.0f shares — pulling %s bid "
                            "in '%s' for %.0f min", imbalance * 100, volume,
                            "NO" if endangered_no else "YES",
                            market.question[:45], self.side_cooldown / 60)
            self._side_blocked_until[blocked] = now + self.side_cooldown
            if newly_blocked and self.on_side_block is not None:
                self.on_side_block(blocked)
            return 0.0, 0.0
        frac = (imbalance - self.flow_widen_thr) / max(
            self.flow_pull_thr - self.flow_widen_thr, 1e-9)
        widen = self.flow_widen_max * frac
        return (0.0, widen) if endangered_no else (widen, 0.0)

    def check_fills(self, fills: list[dict], now: float) -> None:
        recent: dict[tuple[str, str], int] = {}
        for f in fills:
            if now - f["ts"] > self.fill_window or "cid" not in f:
                continue
            if f.get("taker") or f.get("exit"):
                continue
            key = (f["cid"], f["side"])
            recent[key] = recent.get(key, 0) + 1
            if recent[key] >= self.max_fills:
                self._trip(f["cid"], now,
                           f"{recent[key]} {f['side']} fills in "
                           f"{self.fill_window / 60:.0f} min", f["market"])


class MarkoutTracker:
    """Measures adverse selection directly via post-fill price drift."""

    def __init__(self, cfg: dict):
        g = cfg["guards"]
        self.horizons = [float(h) for h in g["markout_horizons_secs"]]
        self.window = float(g["markout_window_minutes"]) * 60
        self.min_samples = int(g["markout_min_samples"])
        self.trip_cents = float(g["markout_trip_cents"])
        self._pending: list[dict] = []
        self._seen_ts = 0.0
        self._samples: dict[str, list[tuple[float, float, float]]] = {}
        self._session: dict[float, list[float]] = {h: [] for h in self.horizons}

    def reload(self, cfg: dict) -> None:
        """Re-read markout thresholds after the controller mutates config.

        Horizons are intentionally left fixed — pending samples are keyed by
        horizon, so changing them mid-session would orphan in-flight markouts.
        """
        g = cfg["guards"]
        self.window = float(g["markout_window_minutes"]) * 60
        self.min_samples = int(g["markout_min_samples"])
        self.trip_cents = float(g["markout_trip_cents"])

    def ingest(self, fills: list[dict]) -> None:
        newest = self._seen_ts
        for f in fills:
            if f["ts"] <= self._seen_ts:
                continue
            newest = max(newest, f["ts"])
            if f.get("taker") or f.get("exit") or "token" not in f or "price" not in f:
                continue
            self._pending.append({
                "ts": f["ts"], "cid": f["cid"], "token": f["token"],
                "price": f["price"], "market": f.get("market", ""),
                "done": set(),
            })
        self._seen_ts = newest

    def resolve(self, mid_lookup, now: float) -> list[dict]:
        """Resolve pending markouts; return newly computed samples for logging."""
        still_pending = []
        resolved: list[dict] = []
        for p in self._pending:
            for h in self.horizons:
                if h in p["done"] or now < p["ts"] + h:
                    continue
                p["done"].add(h)
                mid = mid_lookup(p["token"])
                if mid is None:
                    continue
                markout = mid - p["price"]
                self._samples.setdefault(p["cid"], []).append((now, h, markout))
                self._session[h].append(markout)
                resolved.append({
                    "ts": now, "fill_ts": p["ts"], "cid": p["cid"],
                    "market": p.get("market", ""), "horizon": h,
                    "markout": markout,
                })
            if len(p["done"]) < len(self.horizons):
                still_pending.append(p)
        self._pending = still_pending
        cutoff = now - self.window
        for cid in list(self._samples):
            self._samples[cid] = [s for s in self._samples[cid] if s[0] >= cutoff]
            if not self._samples[cid]:
                del self._samples[cid]
        return resolved

    def market_avg(self, cid: str, horizon: float | None = None) -> float | None:
        """Average markout (price units) for a market at the given horizon."""
        samples = self._samples.get(cid)
        if not samples:
            return None
        h = horizon if horizon is not None else max(self.horizons)
        vals = [m for _, hh, m in samples if hh == h]
        if not vals:
            return None
        return sum(vals) / len(vals)

    def recent_markout(self, horizon: float | None = None) -> tuple[float, int]:
        """Rolling cross-market markout (cents) and sample count.

        Aggregates every market's windowed samples at the long horizon — the
        adaptive controller's primary read on how toxic current flow is.
        Returns (0.0, 0) when no samples are available.
        """
        h = horizon if horizon is not None else (max(self.horizons) if self.horizons else 0.0)
        vals = [m for samples in self._samples.values()
                for _, hh, m in samples if hh == h]
        if not vals:
            return 0.0, 0
        return sum(vals) / len(vals) * 100, len(vals)

    def toxic_markets(self) -> list[tuple[str, float, int]]:
        h_long = max(self.horizons)
        out = []
        for cid, samples in self._samples.items():
            vals = [m for _, h, m in samples if h == h_long]
            if len(vals) < self.min_samples:
                continue
            avg_cents = sum(vals) / len(vals) * 100
            if avg_cents <= self.trip_cents:
                out.append((cid, avg_cents, len(vals)))
        return out

    def reset_market(self, cid: str) -> None:
        self._samples.pop(cid, None)
        self._pending = [p for p in self._pending if p["cid"] != cid]

    def session_stats(self) -> dict[float, tuple[float, int]]:
        return {
            h: ((sum(v) / len(v) * 100) if v else 0.0, len(v))
            for h, v in self._session.items()
        }
