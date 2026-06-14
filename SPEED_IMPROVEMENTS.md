# pmbot — Speed & Latency Improvements

Expert assessment of where this bot is fast enough, where it is not, and
what to change — ordered by **expected PnL impact per engineering hour** for
a reward-farming operation at small capital ($500, 5 markets).

This is **not** an HFT rewrite guide. Polymarket liquidity rewards score resting
orders on in-band uptime and distance from mid, not on sub-millisecond reaction
time. Speed improvements here target three measurable costs:

> pick-off during requotes + time dark after guard trips + hedge delay on toxic inventory

Everything else is secondary unless live data shows a specific bottleneck.

---

## Current latency profile

| Layer | Current behavior | Typical delay |
| --- | --- | --- |
| Book data | CLOB market WebSocket (`books.py`) | ~10–50 ms |
| Fill detection | User WebSocket (`userfeed.py`, live) | ~10–50 ms |
| Quote decision loop | Fixed tick in `main.py::run` | **2.0 s** |
| Requote trigger | Price must drift ≥ `requote_move_cents` (0.4¢) | intentional |
| Order placement | REST via `py_clob_client` in worker thread | **100–300 ms** per op |
| Order cancellation | REST `cancel_orders` / `cancel` fallback | **100–300 ms** |
| Stale quote window | Old quote live until cancel ACK (GTD refresh at ~90s margin) | **100–300 ms** per replace |
| Position reconcile | REST poll every 12 s + WS delta overlay | 0–12 s |
| Forced hedge retry | `FLATTEN_RETRY_SECONDS` in `main.py` | **15 s** |
| Guard pull | Only evaluated on the 2 s quote loop | **0–2 s** after trip condition |

**Verdict:** Fast enough to participate in mid-tier reward pools. Not fast
enough to dominate top pools or compete on tightest-quote share. The largest
remaining speed cost is **requote pick-off**, not loop frequency.

---

## Priority 1 — High ROI (do these first)

These changes directly reduce adverse selection or increase in-band uptime.
Each is compatible with the current Python/asyncio architecture.

### 1.1 Event-driven quote pulls (don't wait for the 2 s loop) — ✅ IMPLEMENTED

**Where:** `main.py::_quote_all`, `risk.py::MarketGuards`, `books.py::_handle`

**Problem:** Guards (`record_mid`, `record_trade`, `check_flow`) can trip
mid-loop, but quotes are only pulled on the next `_quote_all` pass — up to
**2 s** later. During a news move or velocity burst the bot stays quoted on
the endangered side while the book runs through it.

**Why it matters:** Guard trips exist to reduce pick-off; a 2 s reaction delay
partially defeats them. This is the single highest-ROI speed fix that does
**not** require faster order APIs.

**Fix:** When a guard trips (`MarketGuards._trip`, `trip_market`,
`check_flow` pull path, markout trip in `_quote_all`), enqueue an immediate
async cancel for that market's quotes — do not wait for the next loop tick.

```python
# Sketch: guards call back into Bot on trip
async def _pull_market_quotes(self, cid: str) -> None:
    m = self._markets_by_cid.get(cid)
    if m and self.broker.open_quotes(m):
        self.metrics.sample_uptime(cid, False)
        await self._broker_call(self.broker.set_quotes, m, [])
```

Wire `MarketGuards` with an optional `on_trip: Callable[[str], Awaitable]` set
at bot init. Keep the 2 s loop for normal requoting; use events only for pulls.

**Expected impact:** Cuts worst-case toxic exposure window from 2 s → ~300 ms
(cancel round trip). High value on the calmer markets you quote; essential if
you ever loosen `exclude_keywords`.

---

### 1.2 Parallelize per-market order ops within a loop — ✅ IMPLEMENTED

**Where:** `main.py::_quote_all`, `_manage_inventory`; `brokers.py::set_quotes`

**Problem:** `_quote_all` processes 5 markets sequentially. Each market that
needs a requote runs cancel + batch post (~200–600 ms). A full requote cycle
can take **1–3 s** wall time, during which later markets in the iteration
order are quoted on stale books.

**Why it matters:** Latency becomes *correlated with activity* — you requote
because things moved, and markets processed last in the loop are blind longest.

**Fix:** After computing all desired quotes, dispatch order ops concurrently:

```python
tasks = []
for m, final in markets_to_update:
    tasks.append(self._broker_call(self.broker.set_quotes, m, final))
await asyncio.gather(*tasks)
```

Apply the same pattern to `_manage_inventory` exit/hedge calls where safe
(hedges across different markets are independent; two hedges on the same market
are not — group by `condition_id` if needed).

**Expected impact:** Full-loop requote time drops from O(n × latency) to
O(latency). At 5 markets, ~1–2 s saved per heavy requote cycle.

---

### 1.3 Cancel-before-place on every replace (minimize pick-off window)

**Where:** `brokers.py::LiveBroker.set_quotes`

**Problem:** Current flow: evaluate keep/cancel list → batch cancel → batch
place. Correct, but when `to_cancel` and `desired` overlap on the same token,
there is still a window where the old price is live. On a fast move, the stale
bid gets hit between cancel ACK and new order landing.

**Why it matters:** This is the dominant live adverse-selection source for a
reward farmer that requotes ~every 2 s when mid drifts 0.4¢.

**Fix (incremental):**
1. **Prefer widen-over-replace:** if mid moved against you, first try
   cancelling the endangered side only; skip placing the new side until next
   loop if exposure is already elevated.
2. **Fire cancel immediately** when `reconcile_quotes` decides a side must
   change — don't wait to build the full batch for all tokens in the market.
3. Track `cancel_sent_at` per order; suppress new placement until cancel
   confirmed or timeout (200 ms), then place.

**Fix (structural):** Investigate CLOB **modify/replace** endpoints if available
in newer `py_clob_client` versions — atomic replace preserves queue on price
improvements and shrinks the dead zone.

**Expected impact:** Direct reduction in pick-off fills during requotes.
Hard to quantify without live markout data; likely 20–40% of adverse selection.

---

### 1.4 Decouple quote loop from inventory loop timing

**Where:** `main.py::run`

**Problem:** `_quote_all` and `_manage_inventory` run sequentially every 2 s.
Inventory management (passive exits, forced hedges) waits for quoting to finish,
and vice versa. A slow requote cycle delays hedges by seconds.

**Why it matters:** When inventory is toxic, hedge latency is more costly than
quote freshness.

**Fix:** Run two async tasks on different intervals:

| Task | Interval | Purpose |
| --- | --- | --- |
| `_quote_all` | 2 s (or 1 s after 1.5) | quote placement |
| `_manage_inventory` | 0.5–1 s | exits and forced hedges |

Both read shared broker/tracker state; order ops already go through
`_broker_call` / worker threads. Use an `asyncio.Lock` per `condition_id` if
quote and hedge ops on the same market must not overlap.

**Expected impact:** Hedge reaction time drops from (2 s + requote duration) to
≤ 1 s. Reduces forced-hedge slippage on fast-moving complements.

---

## Priority 2 — Medium ROI (after live calibration)

### 2.1 Reduce main loop interval to 1 s (configurable)

**Where:** `main.py` — `LOOP_SECONDS = 2.0`

**Problem:** 2 s is conservative. Reward sampling is per-minute; you do not
need sub-second requotes. But 2 s means up to 2 s out of band after a mid
drift that pushes you outside the reward band without tripping a guard.

**Why it matters:** Reward score is quadratic in distance from mid. Drifting
0.5¢ outside band for 2 s every cycle adds up over a day — especially if
competitors stay tighter.

**Fix:** Add `loop_seconds: 1.0` to `config.yaml`. Keep `requote_move_cents`
at 0.4¢ so queue priority is preserved — faster loop does not mean more
replaces, it means faster detection of *needed* replaces and guard reactions.

**Caution:** Halving the loop doubles REST call volume if most markets requote
every tick. Combine with reconcile logic so unchanged quotes are no-ops (already
done via `reconcile_quotes` + keep logic in `set_quotes`).

---

### 2.2 Persistent HTTP connection pool for CLOB client

**Where:** `brokers.py::LiveBroker` — `self.client` uses `httpx` internally

**Problem:** Each order op may establish a new TCP+TLS session depending on
client configuration. At 100–300 ms per op, connection setup is a meaningful
fraction.

**Fix:**
1. Verify `py_clob_client` reuses connections (check its `httpx.Client` lifecycle).
2. If not, patch or wrap to hold a single long-lived client instance.
3. Run from a low-latency VPS geographically close to Polymarket infra (US East
   typical for US-facing APIs).

**Expected impact:** 20–80 ms shaved per order op. Modest alone; compounds with
1.2 and 1.3.

---

### 2.3 Batch all markets into one `post_orders` call per loop

**Where:** `brokers.py::LiveBroker.set_quotes`

**Problem:** Each market calls `post_orders` independently. Five markets → five
round trips even when parallelized.

**Fix:** Add `LiveBroker.batch_set_quotes(markets: list[tuple[Market, list[Quote]]])`
that collects all new orders across markets into one `post_orders` payload.
Cancels can similarly use one `cancel_orders` with all IDs.

**Expected impact:** Order placement phase: 5 × 200 ms → 1 × 200 ms (sequential
cancel still needed first). Best combined with 1.2.

---

### 2.4 Shorten position poll interval during high activity

**Where:** `main.py` — `POSITION_REFRESH_SECONDS = 12.0`

**Problem:** WS fills are primary in live mode, but when the user feed drops,
12 s until poll-based reconciliation resumes. Inventory skew and hedge
decisions can be wrong for that window.

**Fix:** Adaptive poll interval:
- **12 s** when `ws_fills_active` and inventory is flat
- **3 s** when user feed is down OR `total_inventory_usd` > 50% of cap
- **12 s** otherwise

**Expected impact:** Reduces exposure flapping and double-hedge risk during
feed outages. Not a speed win in the happy path.

---

### 2.5 Pre-sign orders off the critical path

**Where:** `brokers.py::_place_buy`, `set_quotes`

**Problem:** `create_order` (EIP-712 signing) runs synchronously inside
`set_quotes` on the worker thread. Signing adds ~10–50 ms per order.

**Fix:** After computing desired quotes in `_quote_all`, submit signing to a
thread pool immediately. By the time cancel ACK returns, signed orders are
ready to post.

**Expected impact:** Small (~10–50 ms per order) but free once 1.2/1.3 are done.

---

## Priority 3 — Lower ROI / higher effort

These matter for competing on top pools or at larger capital. Unlikely to
change profitability at $500 on mid-tier reward markets.

### 3.1 WebSocket order entry (if/when CLOB supports it)

**Problem:** REST round trips are the hard floor (~100 ms). WebSocket order
entry could cut this to ~20–50 ms.

**Status:** Check current Polymarket CLOB docs / `py_clob_client` changelog.
Not available in the client surface as of the current codebase audit.

---

### 3.2 Colocation / dedicated low-latency host

**Problem:** Running from a home connection or far-region VPS adds 50–200 ms
RTT to every REST call.

**Fix:** Deploy to US-East VPS (e.g. AWS us-east-1, Vultr NJ). Measure RTT to
`clob.polymarket.com` before and after.

**Expected impact:** 50–150 ms per order op. Worth doing before going live;
not worth optimizing further until then.

---

### 3.3 Rewrite hot path in Rust / Go

**Problem:** Python + asyncio + thread pool is fine for 1–5 s loops. It is not
competitive at sub-100 ms reaction times.

**When to consider:** Capital > $5k, quoting top-10 reward pools, or live data
shows you lose >30% of reward share to tighter quoters who react faster.

**When to skip:** Current strategy (0.35 offset, 0.4¢ requote tolerance,
$500, niche pools). Speed is not your binding constraint.

---

### 3.4 Sub-second book-driven requoting

**Problem:** Quotes only update on the main loop, not on every book change.

**Fix:** Register a book listener that recomputes quotes when mid moves ≥
`requote_move_cents` and triggers `_broker_call(set_quotes)`.

**Caution:** Will massively increase order churn and **destroy queue priority**
unless combined with strict reconcile tolerance. For reward farming, event-driven
**pulls** (1.1) are safer than event-driven **replaces**.

---

## What NOT to optimize

| Temptation | Why to skip it |
| --- | --- |
| Sub-second main loop | Increases churn; queue priority matters more than freshness |
| Tighter `requote_move_cents` | More replaces → more pick-off → worse markouts |
| Tighter `offset_frac_of_max_spread` | Higher reward score but more toxic fills; tune on live markouts |
| Faster scanner refresh | Rescan already diffs markets; 30 min is fine |
| Replace asyncio with threads everywhere | Order ops already offloaded; book WS is async — architecture is sound |

---

## Measurement: prove speed changes help

Before and after each change, track these from `metrics.db`:

| Metric | Query / source | Target |
| --- | --- | --- |
| Pick-off rate | Fills where markout @ 30s < −1¢ / total fills | Decrease |
| In-band uptime | `uptime_pct` in daily report | Increase or hold |
| Time dark after guard trip | New: log `guard_trip` → next `record_quotes` delta | < 500 ms |
| Requote pick-off fills | Fills on `_dying` quotes (paper) / cancels-in-flight (live) | Decrease |
| Hedge slippage | avg hedge price − complement mid at hedge time | Decrease |
| Reward share | realized / estimated rewards | Increase |

Add a `events` table to `metrics.py` for guard trips and cancel/post timestamps
if you implement 1.1 or 1.3 — otherwise you cannot tell whether speed work helped.

---

## Go-live workflow (before betting on profitability)

Speed work does not substitute for calibration. Follow this sequence before
scaling capital or chasing Priority 2/3 optimizations.

### Phase 1 — Paper (1–2 weeks)

Run with the realism settings in `config.yaml` (`paper.order_latency_ms`,
`paper.reward_haircut`):

```bash
python -m pmbot.main run
python -m pmbot.main report
```

**Gate:** If paper PnL is not clearly positive after 1–2 weeks, do not go live.
Paper is a ceiling — live will be worse. A flat or negative paper run means
the strategy or market selection is wrong, not that you need more speed.

### Phase 2 — Live small (2+ weeks at $100–200)

Do not start at `capital_usd: 500`. Set capital to **$100–200**, keep
`top_n_markets: 5`, and run live purely to collect data:

```bash
python -m pmbot.main run
python -m pmbot.main report   # daily: realized vs est. rewards, uptime, markouts
```

Track per market (from `metrics.db` + logs):

| Signal | Source | Action threshold |
| --- | --- | --- |
| Realized reward share | `realized_rewards_usd / est_rewards_usd` | Drop market if **< 30%** of estimate |
| Fill toxicity | `MarkoutTracker.market_avg` / session stats | Drop market if avg markout **< −1¢** |
| In-band uptime | `uptime_pct` in report | Drop market if consistently **< 50%** |
| Net PnL | equity PnL + spread capture − hedge costs | Drop market if negative over 1 week |

### Phase 3 — Scale (only proven markets)

After 2+ weeks live:

1. **Drop** any market failing the thresholds above.
2. **Keep** the 2–3 markets with positive net PnL, acceptable markouts, and
   realized reward share ≥ 30% of estimate.
3. **Scale** `capital_usd` toward $500 only on those markets (raise
   `top_n_markets` only if more markets pass the same gates).

Do not scale because paper looked good or because speed improvements shipped.
Scale because live data on specific markets proved the economics.

---

## Strategy tuning (without a full rewrite)

Speed is not the only lever. These changes have equal or higher ROI than
Priority 2/3 for a $500 reward farmer:

| Change | Where | Rationale |
| --- | --- | --- |
| **Event-driven quote pulls** | §1.1 above | Don't wait for the 2 s loop when guards trip |
| **Tighter market selection from live markouts** | `gamma.scan` + post-hoc filter | Stop quoting markets where live markouts stay negative even after adaptive offset; rotate out via rescan or a manual denylist |
| **Wider default offset (0.40–0.45)** | `config.yaml` → `offset_frac_of_max_spread` | At 0.35 on a 3¢ band, gross pair capture (~2¢) is almost fully consumed by a −1.5¢ markout trip. Start wider until markouts prove a market is benign; let `adaptive_offset` tighten per market |
| **Raise `requote_move_cents` slightly (0.5–0.6¢)** | `config.yaml` | Fewer replaces → less pick-off during cancel/post; trades reward score for queue stability |

Speed optimizations beyond Priority 1 have **diminishing returns** for this
strategy unless live data shows you are losing reward share to faster quoters
(realized/estimated ratio OK but uptime high and share still low).

---

## Suggested order of work

**Calibration first (see Go-live workflow above):**

0. **Paper 1–2 weeks** with realism settings — gate on clearly positive PnL.
1. **Deploy to US-East VPS** (3.2) — before live, not after.
2. **Live at $100–200 for 2+ weeks** — measure via `python -m pmbot.main report`.
3. **Drop bad markets; scale only proven ones** toward $500.

**Then speed/strategy (only if live data supports continuing):**

4. **Event-driven guard pulls** (1.1) — biggest safety win, ~1 day of work.
5. **Wider offset (0.40–0.45)** + markout-based market filter — strategy, not code.
6. **Parallel per-market order ops** (1.2) — ~half a day, compounds everything.
7. **Decouple inventory loop** (1.4) — ~half a day, helps hedge latency.
8. **Then consider:** 1.3 cancel-before-place, 2.1 loop → 1 s, 2.3 cross-market batching.
9. **Only if live data shows reward share loss to faster quoters:** 2.2, 2.5, 3.x.

---

## Bottom line

This bot does not need to be fast in the HFT sense. It needs to be:

1. **Slow to requote** (preserve queue) but **fast to pull** (avoid pick-off)
2. **Fast to hedge** when inventory turns toxic
3. **Consistently in-band** (uptime > quote freshness)

The 2 s loop is acceptable for (3). The gaps are (1) — guard pulls wait for
the loop — and requote pick-off during cancel/post. Fix those before chasing
sub-second loops or a language rewrite.

At $500 on mid-tier pools, implementing Priority 1 alone is likely sufficient.
Priority 2 and 3 are for scaling capital or moving upmarket into contested pools.
