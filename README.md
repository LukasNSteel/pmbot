# pmbot — Polymarket market-making bot

Quotes two-sided liquidity (BUY YES + BUY NO) in reward-paying Polymarket
markets to earn the bid-ask spread, daily maker rebates, and a share of each
market's liquidity reward pool. Buying both sides means no inventory is needed
to quote; when both sides fill, the YES+NO pair merges back to $1 and the
spread is realized as profit.

## Quick start (paper mode — no credentials needed)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m pmbot.main scan    # see which markets the bot would quote
python -m pmbot.main run     # paper-trade them with simulated fills
python -m pmbot.main report  # daily PnL decomposition from metrics.db
```

Paper mode tracks simulated fills against the live orderbook, marks positions
to market, and estimates liquidity-reward accrual. State is written to
`data/paper_state.json`; structured metrics go to `data/metrics.db`.

The fill simulation is deliberately pessimistic to make paper PnL a usable
go-live signal (`paper:` section in `config.yaml`):

- **Queue-position model** — fills at your price require the displayed size
  ahead of you to trade first; through-prints fill only the taker's size
- **Order latency** (default 300ms) — new quotes can't fill until placement
  "lands"; replaced quotes stay fillable until the cancel lands, so requoting
  in a moving market gets picked off like it does live
- **Depth-aware hedges** — taker buys walk displayed depth and fill partially
- **Maker fees** charged per fill on fee-bearing markets
- **Reward haircut** (default 0.7×) on estimated reward accrual, since the
  estimator only sees displayed competition

Even so: paper cannot model competitors reacting to your quotes, hidden queue
dynamics, or actual reward payouts. Treat the first live weeks as calibration
regardless of paper results.

## How it picks markets

The scanner pulls up to 2,000 markets (reward-ordered, then volume-ordered),
keeps those paying daily liquidity rewards, and ranks by
**reward pool ÷ book liquidity** — the least crowded reward dollars. Markets
with nonzero maker fees are penalized in the score. Filters (all in
`config.yaml`): minimum pool size, affordable `min_incentive_size`, midpoint
inside 0.15–0.85, not resolving within a few hours.

## How it quotes

- Fair value from microprice (volume-weighted top of book), plus flow-imbalance drift
- Requires a two-sided book with spread ≤ 3× the reward band (no last-trade fallback)
- Bids placed a configurable fraction of the reward band away from fair value
- Per-market offset adapts from markout data (widen when picked off, tighten when benign)
- Capital allocated by estimated reward $/day per $ committed per market
- Inventory skew shifts both quotes to mean-revert accumulated exposure
- GTD orders (90s TTL) self-expire on crash; the requote loop refreshes them

## Risk controls

Quote sizes, inventory caps, and the forced-hedge threshold scale with
equity relative to `capital_usd` (clamped 0.5×–10×). Daily loss limits use
a median-smoothed equity mark. Dollar values below are at the `capital_usd: 500`
baseline; daily loss limits stay absolute.

| Control | Default | Behavior |
| --- | --- | --- |
| Per-market inventory cap | $60 | side dropped / reduce-only |
| Total inventory cap | $250 | warn + reduce |
| Daily loss limit | $25 | cancel quotes, pause until next UTC day (inventory management continues) |
| Hard kill | $50 | cancel all, exit process |
| Passive exit | >$15 unpaired | rest a reduce-only SELL on the excess token at its ask |
| Forced hedging | >$15 unpaired for 90s | cross the spread on the complement book, merge the pair to $1 |
| Market-exit liquidation | market drops out of the quote set | force-flatten remaining unpaired inventory |
| Resolution de-risk window | last 12h | inventory cap shrinks (to 25%) and quotes widen (up to 2c) |
| Resolution exit window | last 2h | stop quoting, force-flatten everything |
| Volatility guard | 3c / 60s | pull quotes from that market for 45 min |
| Same-side fill breaker | 3 fills / 15 min | pull quotes from that market for 45 min |
| Trade velocity breaker | 8 trades / 10s | pull quotes from that market for 45 min |
| Directional flow breaker | 5 consecutive same-side taker trades | pull the endangered bid only, 10 min |
| Flow imbalance (VPIN-lite) | >60% one-way volume / 5 min | widen the endangered bid up to 2c; >85% pulls it for 10 min |
| Markout guard | avg markout ≤ −1.5c @5 min over ≥5 fills | pull quotes from that market for 45 min |
| Quote fading | 0.5c per recent fill (cap 2c) | each fill pushes that side's next bid away |
| Latency kill switch | feed silent 25s | pull all quotes until data flows again |
| Live mid range | 0.15–0.85 | stop quoting markets drifting toward resolution |
| Neg-risk event grouping | auto by event_id | combined exposure cap across correlated markets |

Forced hedging only fires when the complement book's spread is at most
`flatten_max_spread_cents` (4c) — it retries every 15s rather than dumping
into an empty book.

Ctrl-C always cancels all resting orders before exiting.

## Metrics & reporting

All fills, quote changes, hedges, merges, equity snapshots, in-band uptime,
and reward accrual are logged to SQLite (`data/metrics.db`).

```bash
python -m pmbot.main report
```

Shows daily PnL decomposition: spread capture (merges), hedge costs,
estimated vs realized rewards, equity PnL, maker fill count, in-band uptime %.

## Going live

1. Copy `.env.example` to `.env` and fill in:
   - `POLYMARKET_PRIVATE_KEY` — email/Magic logins: Settings → Export Private Key
   - `POLYMARKET_FUNDER` — your Polymarket deposit address (profile page)
2. Set `live.signature_type` in `config.yaml`: `1` for email/Magic login,
   `2` for browser-wallet login, `0` for a plain EOA trading directly
3. Fund the account with USDC on Polygon
4. Set `mode: live` in `config.yaml` and run `python -m pmbot.main run`

In live mode:
- Order operations run off the asyncio event loop (no blocking HTTP during quoting)
- Fills arrive in real time over the authenticated user WebSocket
- Open orders are reconciled against the exchange every ~30s
- GTD orders self-expire on crash (90s dead-man switch)
- Positions polled every ~12s with WS-fill delta reconciliation
- YES+NO pairs merged on-chain every ~60s (once `merge_min_pairs` accumulate)

Start with a small `capital_usd` and watch the first sessions closely.

## Testing

```bash
pytest tests/ -v
```

## Project layout

```
pmbot/
  gamma.py     market scanner (Gamma API, fee rates)
  books.py     orderbook tracker (WebSocket + REST fallback)
  userfeed.py  authenticated user WebSocket (real-time fills, live mode)
  strategy.py  quote computation, microprice, adaptive offset, reward estimator
  brokers.py   PaperBroker (queue fill model) / LiveBroker (GTD, batch, reconcile)
  merger.py    on-chain YES+NO pair merging (pUSD adapters, live mode)
  risk.py      loss limits, inventory caps, guards, markouts
  metrics.py   SQLite logging, uptime, PnL decomposition, report
  main.py      CLI orchestrator (scan / run / report)
```
