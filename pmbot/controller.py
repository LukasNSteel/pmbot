"""Adaptive controller: self-manages config from capital and market conditions.

The bot's profit hinges on two opposing forces:

  * tighter quotes / faster hedging  -> more reward share, but more pick-off
  * wider quotes / patient exits      -> less adverse selection, fewer rewards

The right balance is not static — it depends on how much capital is at risk and
how toxic current order flow is. This controller closes that loop. Every
``interval_minutes`` it reads live equity and the rolling markout (post-fill
price drift, the direct measure of adverse selection) and rewrites a small set
of config knobs in place:

  capital tier   -> top_n_markets, per-market inventory cap, loss limits
  toxicity       -> quote offset, passive-exit patience, hedge spread ceiling,
                    markout trip sensitivity

Every adjustment is bounded by anchors in ``config.controller`` so the system
can adapt smoothly (``smoothing``) without ever leaving operator-defined limits.
Disabled by default — set ``controller.enabled: true`` to turn it on.
"""

from __future__ import annotations

import logging

log = logging.getLogger("pmbot.controller")

# Knobs interpolated between the calm and toxic anchors. Each entry is
# (config_section, key, display_format).
_TOXICITY_KNOBS = [
    ("quoting", "offset_frac_of_max_spread", "{:.3f}"),
    ("risk", "flatten_after_secs", "{:.0f}"),
    ("risk", "flatten_max_spread_cents", "{:.1f}"),
    ("guards", "markout_trip_cents", "{:.2f}"),
    # Drop toxic markets faster (fewer fills needed to trip) under bad flow.
    # Interpolated as a float, read back as int by MarkoutTracker.
    ("guards", "markout_min_samples", "{:.0f}"),
]

# Structural knobs a capital tier may set (all read live by the bot/scanner).
# Depth-first scaling: as equity grows we raise per-market quote size and its
# capital cap AHEAD of the market count (see REPORT_reward_selection.md §5) —
# reward is ~linear in our size while extra markets feed the toxic tail.
_TIER_KNOBS = [
    ("scanner", "top_n_markets", int),
    ("quoting", "size_mult_of_min", float),
    ("quoting", "max_capital_per_market", float),
    ("risk", "max_inventory_usd_per_market", float),
    ("risk", "daily_loss_limit_usd", float),
    ("risk", "hard_kill_loss_usd", float),
]


class AdaptiveController:
    """Rewrites config knobs in place from equity and rolling markout."""

    def __init__(self, cfg: dict, guards, markouts, metrics=None):
        self.cfg = cfg
        self.guards = guards
        self.markouts = markouts
        self.metrics = metrics
        cc = cfg.get("controller") or {}
        self.cc = cc
        self.enabled = bool(cc.get("enabled"))
        self.interval = float(cc.get("interval_minutes", 10)) * 60.0
        self.smoothing = max(0.0, min(1.0, float(cc.get("smoothing", 0.5))))
        self.calm_cents = float(cc.get("markout_calm_cents", 0.0))
        self.toxic_cents = float(cc.get("markout_toxic_cents", -3.0))
        self.min_samples = int(cc.get("min_markout_samples", 4))
        self.calm = cc.get("calm") or {}
        self.toxic = cc.get("toxic") or {}
        self.tiers = sorted(
            cc.get("capital_tiers") or [],
            key=lambda t: float(t.get("min_equity_usd", 0)),
        )
        self._last_eval = 0.0
        self._started = False
        # Exposed for status display.
        self.active_tier_equity: float | None = None
        self.toxicity: float = 0.0
        self.last_markout_cents: float = 0.0
        self.last_markout_n: int = 0

    def maybe_apply(self, now: float, equity: float) -> bool:
        """Re-evaluate if enabled and the interval has elapsed (or first run)."""
        if not self.enabled:
            return False
        if self._started and now - self._last_eval < self.interval:
            return False
        self._last_eval = now
        self._started = True
        self.apply(equity)
        return True

    def apply(self, equity: float) -> None:
        changes: list[str] = []
        self._apply_capital_tier(equity, changes)
        self._apply_toxicity(changes)
        if changes:
            self.guards.reload(self.cfg)
            self.markouts.reload(self.cfg)
            log.info("controller adjusted: %s", "  ".join(changes))

    # ---------------------------------------------------------------- capital

    def _select_tier(self, equity: float) -> dict | None:
        if equity != equity:  # NaN equity (unknown balance) — don't retier
            return None
        chosen = None
        for tier in self.tiers:
            if equity >= float(tier.get("min_equity_usd", 0)):
                chosen = tier
        return chosen

    def _apply_capital_tier(self, equity: float, changes: list[str]) -> None:
        tier = self._select_tier(equity)
        if tier is None:
            return
        self.active_tier_equity = float(tier.get("min_equity_usd", 0))
        for section, key, cast in _TIER_KNOBS:
            if key not in tier:
                continue
            target = cast(tier[key])
            cur = self.cfg[section].get(key)
            if cur is None or cast(cur) != target:
                self.cfg[section][key] = target
                changes.append(f"{section}.{key}={target}")

    # --------------------------------------------------------------- toxicity

    def _toxicity(self) -> float:
        """Fraction in [0, 1]: 0 = calm anchors, 1 = toxic anchors."""
        markout, n = self.markouts.recent_markout()
        self.last_markout_cents = markout
        self.last_markout_n = n
        if n < self.min_samples:
            # Not enough evidence yet — stay calm rather than overreact.
            return 0.0
        span = self.calm_cents - self.toxic_cents
        if span <= 0:
            return 0.0
        t = (self.calm_cents - markout) / span
        return max(0.0, min(1.0, t))

    def _apply_toxicity(self, changes: list[str]) -> None:
        if not self.calm or not self.toxic:
            return
        t = self._toxicity()
        self.toxicity = t
        for section, key, fmt in _TOXICITY_KNOBS:
            if key not in self.calm or key not in self.toxic:
                continue
            calm_v = float(self.calm[key])
            toxic_v = float(self.toxic[key])
            target = calm_v + t * (toxic_v - calm_v)
            cur = float(self.cfg[section][key])
            new = cur + self.smoothing * (target - cur)
            # Snap to target once within smoothing noise to avoid endless drift.
            if abs(new - target) < abs(target - calm_v) * 0.01 + 1e-9:
                new = target
            if abs(new - cur) > 1e-9:
                self.cfg[section][key] = new
                changes.append(f"{section}.{key} {fmt.format(cur)}\u2192{fmt.format(new)}")

    # ----------------------------------------------------------------- status

    def status_line(self) -> str:
        tier = (f"\u2265${self.active_tier_equity:.0f}"
                if self.active_tier_equity is not None else "n/a")
        return (f"controller: tier {tier}  toxicity {self.toxicity:.0%}  "
                f"markout {self.last_markout_cents:+.1f}c (n={self.last_markout_n})  "
                f"offset {self.cfg['quoting']['offset_frac_of_max_spread']:.2f}  "
                f"flatten {self.cfg['risk']['flatten_after_secs']:.0f}s")
