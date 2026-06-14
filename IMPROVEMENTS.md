# pmbot — Review Findings & Required Changes

Expert review of the market-making bot, organized by priority. Each item states
**what to change**, **where**, and **why it matters for live profitability**.

The strategy's economics in one line:

> PnL ≈ reward capture − adverse selection − forced-hedge costs − (time out of band)

At `offset_frac_of_max_spread: 0.35` on a typical 3c band, a completed YES+NO
pair nets ~2c gross, and the markout trip threshold (−1.5c) consumes nearly all
of that. This is a **reward-farming operation, not a spread-capture operation**
— every change below either protects the reward income or reduces the
adverse-selection cost that eats it.

---

## Priority 1 — Live-loss bugs (fix before going live)

### 1.1 Blocking HTTP inside the asyncio event loop

**Where:** `brokers.py` — `LiveBroker.set_quotes`, `set_exit`, `taker_buy`,
called synchronously from `main.py::_quote_all`.

**Problem:** Each order placement/cancel is a synchronous `py_clob_client`
HTTP round trip (~100–300ms) running on the event loop. A requote across 5
markets can freeze the loop for seconds, during which the WebSocket book feed
is not processed.

**Why it matters:** This makes the bot's data staleness *correlate with its own
activity* — the worst possible latency profile for a market maker. You requote
because the market moved, and the requote itself blinds you to the next move.

**Fix:** Wrap order operations in `asyncio.to_thread` (already done for
`refresh_state`), or use the CLOB batch order endpoints. Order ops must never
block book processing.

### 1.2 No open-order reconciliation — phantom orders after fills

**Where:** `brokers.py` — `LiveBroker._open_orders` / `_exit_orders`.

**Problem:** `_open_orders` is only mutated by `set_quotes`. When an order
fills, the user feed updates positions but never removes the order from the
local map, so the bot believes a phantom quote is still resting. Today this is
accidentally masked because `fade_cents_per_fill` (0.5c) >
`requote_move_cents` (0.4c), forcing a replace one loop later — but:

- partial fills leave wrong sizes resting indefinitely (overstating reward
  score and understating risk),
- the self-heal silently breaks if anyone tunes those two config values past
  each other.

**Why it matters:** The bot's picture of its own resting orders is the
foundation for everything — requoting, reward estimation, exposure. It must be
authoritative.

**Fix:** On each user-feed fill, decrement/remove the matched order from
`_open_orders`. Additionally, reconcile periodically against the exchange via
`get_orders` (catches fills that arrive while the user feed is down).

### 1.3 `PAUSE_DAY` abandons inventory management

**Where:** `main.py::run`:

```python
if action == RiskAction.PAUSE_DAY:
    self.broker.cancel_all()
    continue
```

**Problem:** The `continue` skips `_manage_inventory`, and `cancel_all()` also
kills passive exit sells. At the exact moment the daily loss limit trips —
which almost always means the bot is holding toxic unpaired inventory — it
stops hedging and lets a binary position ride for up to 24 hours. It also
calls `cancel_all()` every 2s while paused, hammering the API.

**Why it matters:** The loss limit exists to cap damage; in its current form
it can *amplify* damage by freezing the unwind machinery.

**Fix:** While paused: stop placing new quotes, but keep running
`_manage_inventory` (passive exits + forced hedges). Call `cancel_all()` once
on entering the paused state, not every loop.

### 1.4 Orphaned GTC orders on crash

**Where:** `brokers.py` — all orders posted as `OrderType.GTC`.

**Problem:** Ctrl-C cancels cleanly, but a SIGKILL / panic / power loss leaves
GTC orders resting on the exchange with no management. An unmanaged stale bid
in a moving market is free money for someone else.

**Why it matters:** Tail risk. The one time the process dies during a news
event, unmanaged quotes can absorb the full move.

**Fix:** Place quotes as GTD with a 60–120s expiry and let the requote loop
refresh them. This is a free dead-man's switch.

### 1.5 WS-fill vs position-poll race

**Where:** `brokers.py::refresh_state` vs `record_user_fill`.

**Problem:** `refresh_state` wholesale replaces `_positions` from the data
API, which can lag real-time WebSocket fills by seconds. A stale poll snapshot
momentarily erases fills the bot already knows about, making exposure flap.

**Why it matters:** Exposure flapping can double-fire forced hedges (paying
the spread twice) or flip the inventory skew back and forth.

**Fix:** Treat WS fills as deltas on top of the last poll snapshot: apply any
fill timestamped after the snapshot, or ignore poll data older than the last
WS fill.

### 1.6 Paper fill model overstates live PnL — don't use it as the go-live signal

**Where:** `brokers.py::PaperBroker._on_trade` / `check_crossed_books`;
`main.py::_sample_rewards`.

**Problem:** Paper fills trigger on any trade at/below the quote price, at
full size, with implicit front-of-queue priority. Live, queue position means
fills skew toxic — you get filled when nobody else wanted it and miss the
benign flow. The reward estimator also treats the entire competing book as one
participant, overstating share.

**Why it matters:** Paper profitability is necessary but not sufficient.
Going live on the strength of paper PnL alone is the classic way this strategy
loses money.

**Fix:** Only count a paper fill when a trade prints *strictly below* the
quote price (price priority guarantees the fill); add a queue-position model
using displayed size at the level. Treat live weeks 1–2 as calibration, not
production.

---

## Priority 2 — Operational improvements

### 2.1 Rescan cancels everything every 30 minutes

**Where:** `main.py::_rescan`.

**Problem:** Every rescan calls `cancel_all()` and tears down/rebuilds the
BookTracker even when the market set is unchanged. The bot loses queue
priority everywhere and goes dark for the reconnect window — twice an hour.

**Why it matters:** Queue priority is earned over time and is directly worth
fill quality; in-band uptime is directly worth reward dollars. Both are
sacrificed for nothing 48 times a day.

**Fix:** Diff the new market set against the old. Keep orders and WS
subscriptions for unchanged markets; only add/remove the delta.

### 2.2 `taker_buy` (live) reports phantom fills

**Where:** `brokers.py::LiveBroker.taker_buy` — `return size` regardless of
what the FAK actually filled.

**Problem:** The "FORCED HEDGE" success log can lie, and `_over_since` is
cleared on a fill that may not have happened, delaying the real hedge by
another full cycle.

**Fix:** Parse the actual matched amount from the order response and return
that.

### 2.3 Equity marks are vulnerable to thin-book noise

**Where:** `brokers.py::equity` — positions marked at raw mid.

**Problem:** A single flickering thin book can swing marked equity enough to
trip the daily-loss pause or hard kill on noise rather than real losses.

**Fix:** Use a clamped or time-smoothed mark specifically for the loss-limit
calculation (trading decisions can keep using the live mid).

### 2.4 Scanner pagination can miss the best targets

**Where:** `gamma.py::fetch_reward_markets` — top 1,000 by `volume24hr`.

**Problem:** The strategy's own scoring (`pool / liquidity`) favors
low-volume, high-pool markets — exactly the ones most likely to fall outside
a volume-ordered top-1,000 window.

**Fix:** Scan deeper, or page with an ordering aligned to reward criteria.

### 2.5 No tests on live-trading-critical logic

**Where:** Everywhere; minimum surface: `strategy.reconcile_quotes`, the
`LiveBroker` order-diff logic, `userfeed._handle_trade` parsing, the guards.

**Why it matters:** These functions encode the order state machine. A silent
regression here loses real money before it's noticed.

### 2.6 Documentation drift

**Where:** `README.md` says flatten threshold $30; `config.yaml` says $15.
Keep in sync — operational docs you don't trust are docs you stop reading.

---

## Priority 3 — Strategy / edge improvements (where profitability is decided)

### 3.1 The scanner systematically selects for unreliable mids

**Where:** `gamma.py::scan` scoring + `books.py::Book.mid` fallback.

**Problem:** Ranking by `pool / liquidity` favors thin books — precisely where
`(best_bid + best_ask) / 2` is noise and where `mid` can fall back to an
arbitrarily stale `last_trade_price`. Quoting a fixed offset from a garbage
mid in a 10c-wide book makes the bot the best bid by miles, to be lifted by
the first informed seller.

**Fix:** Hard gate: require a two-sided book with spread below a sanity
threshold (e.g. 2–3× the reward band) to quote at all; never quote off the
last-trade fallback.

### 3.2 Quote around fair value, not the mid

**Where:** `strategy.compute_quotes` centers on `yes_book.mid`.

**Problem:** Symmetric quoting around the raw mid is pure rent collection; it
makes no attempt to avoid the side about to get hit.

**Fix (cheap, high value):** Use the microprice as the quote center:

```python
fair = (best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)
```

Additionally, feed the signed taker-flow imbalance (already computed for the
VPIN guard) into the quote center as a continuous drift term, instead of only
using it as a binary widen/pull. This is the single biggest expected-PnL
improvement available.

### 3.3 Use markout data as a controller, not just a circuit breaker

**Where:** `risk.py::MarkoutTracker` + `strategy.compute_quotes`.

**Problem:** Per-market average markouts are collected and then discarded
unless they breach −1.5c. All the information between "fine" and "tripped" is
wasted.

**Fix:** Make the offset adaptive per market:
`offset_m = base_offset + k · max(0, −avg_markout_m)`, and allow *tightening*
below base where markouts are persistently benign. The quadratic reward
scoring `((v−s)/v)²` means a competitor at the tick scores ~2.4× a quote at
35% of band — reward share is won by being tighter exactly where flow is
harmless.

### 3.4 Allocate capital by expected reward per dollar, not flat sizing

**Where:** `strategy.compute_quotes` (`size_mult_of_min`) + `gamma.scan`.

**Problem:** Every market gets the same size regardless of pool size or
competition. With $500 across 5 markets, spreading capital into pools where
the bot's share is rounding error wastes the binding constraint (capital).

**Fix:** At scan time, run `estimate_reward_share` against real books to get
estimated $/day per $ committed for each candidate; size positions
proportionally, subject to existing caps.

### 3.5 Theme caps are dead code as configured

**Where:** `config.yaml` — every `theme_groups` keyword also appears in
`scanner.exclude_keywords`, so themed markets never enter the quote set.

**Problem:** The real correlation risk is *neg-risk event groups*: multiple
markets on the same event are mechanically correlated, and hand-tuned keywords
will never keep up.

**Fix:** Group automatically by event / neg-risk market group from the Gamma
API and apply the combined-exposure cap to those groups.

### 3.6 Measure actual rewards and in-band uptime

**Where:** `LiveBroker.accrue_rewards` is a no-op; nothing tracks uptime.

**Problem:** Two of the three terms that decide profitability (reward capture
share, in-band uptime) are unmeasured in live mode. If the live reward share
is half the estimate, the whole market-selection calculus changes. Every
minute a guard has quotes pulled is foregone income — if guards keep the bot
dark 40% of the time, loosening them may net out positive even with worse
markouts. That trade-off is measurable; measure it.

**Fix:** Pull realized reward payments daily (data API earnings endpoints) and
reconcile against the estimator. Track % of minutes with two-sided in-band
quotes per market (reward sampling is random per minute).

### 3.7 PnL decomposition + structured logging

**Where:** New; `data/paper_state.json` keeping the last 200 fills is not
analysis-grade.

**Problem:** "Ensure it is profitable" is impossible without attribution. A
flat PnL line could be healthy rewards minus tolerable toxicity, or zero
rewards plus luck — the responses are opposite.

**Fix:** Log every quote update, fill, hedge, and merge to structured storage
(SQLite or parquet). Decompose daily PnL into: spread capture (merged pairs),
inventory mark PnL, forced-hedge costs, rewards. At $500 capital, the dollar
PnL of the first month is irrelevant; the per-component statistics are the
product.

### 3.8 Verify the fee/rebate schedule

**Where:** README assumes maker rebates exist.

**Fix:** Confirm the current fee/rebate schedule on the specific markets
quoted and build the actual numbers into the market-selection math rather than
assuming.

---

## Suggested order of work

1. **Fix 1.1–1.3** (event-loop blocking, order reconciliation,
   pause-abandons-inventory) — these are live-loss bugs, not optimizations.
2. **Add GTD expiry (1.4) and the rescan diff (2.1).**
3. **Instrument everything (3.6, 3.7):** structured logging, PnL
   decomposition, realized-reward reconciliation, uptime tracking.
4. **Run live small** ($100–200) for 2+ weeks purely to collect
   markout/reward/uptime data.
5. **Then do the alpha work (3.1–3.4),** tuned on real fill data rather than
   paper simulation.

**Bottom line:** the risk plumbing is good enough to keep losses small, but
profitability will be decided by reward capture share and in-band uptime
versus adverse selection — and the bot currently measures none of those three
terms in live mode. Fix the bugs, instrument everything, and let the data
decide which markets and offsets actually pay.
