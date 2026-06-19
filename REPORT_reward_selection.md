# Reward-prioritized selection & position scaling — research report

**Question (from the desk).** Keep the market filter for *eligibility*, but
then prioritize the eligible markets where we would earn the **highest rewards**
— today we sit in markets where we contribute almost nothing to in-band
liquidity, so we capture almost none of the pool. Make safety a first-class
constraint. Back-test it. And separately: as capital grows, instead of quoting
**more markets**, should we hold **bigger positions** per market (today's cap is
~50, what about 100)?

**TL;DR.**

1. We are currently **~0.5% of the in-band reward score** in the markets we
   quote (realized rewards are only **20% of the estimator's number** —
   $5.22 vs $26.70 over the live window). That means we sit deep in the
   **linear** part of the reward curve: **doubling our quote size roughly
   doubles our reward**, with negligible diminishing returns.
2. Because we are this small, **reward density (pool ÷ liquidity) and expected
   captured reward (pool × our share) rank the eligible markets identically.**
   The selection fix that matters is **(a) weighting absolute pool and our
   *capturable* share as we size up, and (b) screening toxicity** — not swapping
   one ratio for another.
3. **The entire profit swing in the back-test comes from selection *quality*
   (avoiding toxic markets), not from the reward mechanism.** Our live losses
   were adverse-selection / forced-hedge bleed in Toy Story & Trump-style books,
   not low rewards.
4. **Depth beats breadth as capital grows — up to a diversification floor.**
   Spreading a bigger book across *more* markets forces capital down the quality
   ladder (and there are only ~8 eligible markets right now, so breadth even
   leaves capital idle). Concentrating into a **few vetted markets with larger
   positions** earns more *and* has a better tail. The risk-adjusted sweet spot
   is **2–3 markets, not 1, and not 8.**

Reproduce everything with:

```bash
.venv/bin/python scripts/reward_selection_study.py     # this report's numbers
.venv/bin/python scripts/backtest.py                   # fill replay (existing)
```

---

## 1. How the reward actually works (and why we earn so little)

Polymarket scores each resting order inside the reward band with
`S = ((v − s)/v)² · size`, where `v` is the band width (cents) and `s` is our
distance from the mid. Our payout each epoch is our score divided by the **total
in-band score** (us + every competitor):

```
our_reward = pool × q_ours / (q_ours + q_competitors),   q_ours ∝ size
```

The share is **concave in our size** but **linear when we are small**. From the
live DB:

| measured (live, 81h, ~$100) | value |
| --- | --- |
| realized rewards | **$5.22** |
| estimator's rewards | $26.70 |
| realized ÷ estimate | **20%** |
| implied in-band score share | **~0.5%** (we are ~1/195 of the book) |
| long-horizon markout (mean / worst) | **−1.59c / −13c** |
| pairs merged / hedge notional | 1,150 / $658 |

At a **0.5% share** we are nowhere near saturation, so:

```
size  50 → 0.51% share   (1.0×)
size 100 → 1.02% share   (2.0×)   ← "50 → 100" almost exactly doubles reward
size 200 → 2.02% share   (3.9×)
```

**This is the single most important fact in the report.** The reason we "don't
make much reward" is not bad market selection per se — it is that we quote the
**minimum size (50 shares)** and are therefore a rounding error in the pool.
The most direct lever to "make more reward" is to **contribute more size**, and
because we are in the linear regime that conversion is ~1:1.

---

## 2. Selection: density vs expected captured reward

The scanner currently ranks eligible markets by **reward density = pool ÷ book
liquidity** (`pmbot/gamma.py`). The desk's instinct is to rank by **expected
captured reward = pool × our share** instead. Running both on a **live scan of
633 markets → 8 eligible** today:

```
--- BY DENSITY (current) ---            --- BY EXPECTED CAPTURE @ size 50 ---
Mazzei OK Gov   pool$461 cap$3.17/d     Mazzei OK Gov   pool$461 cap$3.17/d
Avila Chevalier pool$405 cap$2.93/d     Avila Chevalier pool$405 cap$2.93/d
Espaillat NY    pool$395 cap$2.49/d     Espaillat NY    pool$395 cap$2.49/d
...                                     ... (identical order)
```

**They produce the same ranking.** The reason is algebraic: when our share is
tiny, `capture = pool · αs/(αs + γ·liq) ≈ (αs/γ)·(pool/liq)` — i.e. expected
capture is *proportional to density*. So at our current size, switching the
ranking metric changes nothing.

The two metrics **diverge only once our size is large enough that share stops
being tiny** — exactly the regime the 50→100→200 position change moves us into:

```
--- BY EXPECTED CAPTURE @ size 250 ---
Mazzei OK Gov   share 10.5%  cap$14.54/d
Avila Chevalier share 11.0%  cap$13.34/d
Adrian Boafo    share  4.3%  cap$ 7.04/d   ← high pool ($542) but deepest book,
                                             so capture saturates slower
```

**Conclusion for selection:** adopt the **expected-captured-reward** ranking
(it is the economically correct objective and is free to compute), but
understand that its *benefit* only switches on once we size up. The bigger,
immediate selection win is the **toxicity screen** below.

---

## 3. Safety is the real PnL driver — back-test

The fill-replay back-test (`scripts/backtest.py`) on the live window shows
trading cash of **−$48.89**, with the losses concentrated in **event/observation
markets** (Trump −$17, Toy Story −$27 across three books) that throw off large
forced-hedge spend. Rewards over the same window were only ~$5 realized — far too
small to offset that bleed.

The Monte-Carlo in `scripts/reward_selection_study.py` makes the mechanism
explicit by running every capital allocation under two selection regimes:

- **OLD** — raw empirical markout sample (the toxic −13c tail intact).
- **NEW** — reward-prioritized selection of slow, low-turnover markets **plus
  the toxicity guard**, which clips the worst markout tail at −3c.

`PnL$/d` = reward + Σ pairs·(markout − hedge slip), bootstrapped from our own
markout distribution; `CVaR5%` is the mean of the worst 5% of days (lower =
worse tail). Reward is haircut by a 0.30 realization factor (uptime/eligibility)
so the dollars are honest; the haircut cancels in every comparison.

```
                         OLD selection            NEW selection (reward-priority + guard)
CAPITAL $500             PnL$/d  P(profit) CVaR    PnL$/d  P(profit) CVaR
8 mkt × 50sh (breadth)   -8.20    11%     -25.3    -3.89    17%     -12.0
5 mkt × 100sh            -6.16    32%     -34.1    -0.80    45%     -13.5
3 mkt × 150sh            -3.49    46%     -38.0    +1.34    59%     -13.4
2 mkt × 200sh (depth)    -2.21    49%     -41.4    +1.75    58%     -12.9   ← best
2 mkt × 250sh            -3.37    48%     -52.3    +1.96    54%     -16.5
```

**Read this top-to-bottom and left-to-right:**

- **Left → right (OLD → NEW): the sign flips.** Selection quality — *not* the
  reward formula, *not* the position size — is what turns the book from a daily
  loser into a daily winner. This is the headline. Prioritising rewards only
  pays once we have stopped feeding the toxic tail.
- **Top → bottom (breadth → depth): depth wins once selection is fixed.** Under
  NEW, max breadth (8×50) is the *worst* row on every axis and only deploys
  $400 of the $500 (there are just 8 eligible markets — **breadth literally runs
  out of good markets and leaves capital idle**). Concentrating into 2–3 markets
  with 150–200-share positions earns more *and* has the tightest tail.

---

## 4. Breadth vs depth: more markets or bigger positions?

This is the desk's second question, and the answer is **bigger positions, with
a floor on diversification.** Three forces:

1. **Reward is ~linear in size for us (Section 1)**, so there is essentially
   *no* concavity penalty for concentrating size. (If we were a large share of a
   pool, breadth would win on pure reward — we are not.)
2. **The eligible universe is small and quality-ranked.** Adding market #4, #5,
   #8 means quoting progressively worse pools — and, historically, the toxic
   ones. Every extra market is also an extra independent draw on the adverse-
   selection lottery that produced our losses.
3. **Safety.** Fewer markets = less adverse-selection surface, less theme/neg-
   risk correlation stacking, simpler inventory and hedge management on a small
   wallet. But **one** market is too few — a single toxic surprise has nowhere to
   diversify against (see the worsening CVaR at 2×250 vs 2×200).

| capital | today (breadth) | recommended (depth) | why |
| --- | --- | --- | --- |
| $100 | 2 × 50 | **2 × 50** (hold) | edge is marginal; keep diversification |
| $200–500 | 3–4 × 50 | **2–3 × 100–150** | depth ≈ 2× reward, better tail |
| $750+ | 3–4 × 90 | **3 × 150–250** | stay in vetted markets, scale size |

The "50 → 100" change the desk proposed is correct, and the back-test says push
it further (toward 150–200) **as long as** (a) selection toxicity screening is on
and (b) we never drop below ~2 markets.

---

## 5. Recommendations (priority order)

1. **Keep ≥ 2 markets always; grow position size, not market count, as capital
   grows.** Re-shape the controller's capital tiers so the wallet scales
   `max_capital_per_market` (and quote size) ahead of `top_n_markets`. Concretely
   add `quoting.max_capital_per_market` to the controller's tier knobs and set:

   ```yaml
   capital_tiers:
     - {min_equity_usd: 0,    top_n_markets: 2, max_capital_per_market: 50,  max_inventory_usd_per_market: 30}
     - {min_equity_usd: 250,  top_n_markets: 2, max_capital_per_market: 100, max_inventory_usd_per_market: 60}
     - {min_equity_usd: 750,  top_n_markets: 3, max_capital_per_market: 175, max_inventory_usd_per_market: 120}
     - {min_equity_usd: 2000, top_n_markets: 3, max_capital_per_market: 300, max_inventory_usd_per_market: 250}
   ```

   and let quote size track the cap (raise `quoting.size_mult_of_min`, or enable
   bounded `risk.scale_with_equity`) so we actually *use* the bigger cap.

2. **Switch the scanner ranking to expected captured reward.** Score eligible
   markets by `pool × αs/(αs + γ·liquidity)` (a size-aware version of density),
   not raw density. It is the correct objective and starts to matter the moment
   we size up. Free to compute from fields the scanner already has.

3. **Selection toxicity screen is the biggest safety win** — and it is already
   mostly built (`exclude_keywords`, `toxicity_turnover_penalty`, the markout
   guard). Keep prioritising **slow, low-turnover markets** (political primaries,
   long-dated yes/no) and keep `min_pool_to_liquidity` / `min_liquidity` so we
   never chase thin toxic books for density.

4. **Couple every size increase to the existing guards.** Bigger positions mean
   bigger per-fill damage if a market turns. The markout guard, per-market
   inventory cap, theme cap, and forced-hedge thresholds must scale *with* size —
   the tiers above already raise `max_inventory_usd_per_market` in step.

## 6. Caveats

- We have **no historical order books**, so we cannot simulate counterfactual
  *fills*. The reward math (Section 1–2) is exact; the PnL Monte-Carlo
  (Section 3–4) bootstraps our own 27-sample markout distribution — directionally
  reliable, not a guarantee. Treat absolute dollars as illustrative and the
  *comparisons* as the signal.
- The competition model `q_comp = γ·liquidity` is a calibrated proxy. The
  qualitative conclusions (linear regime, density≈capture when small, depth>breadth
  after the toxicity fix) are robust across the γ range tested; precise shares are
  not.
- Realized rewards are paid per UTC day only while we are actually in-band, so
  real-world capture also depends on **uptime** — another reason to prefer fewer,
  more reliably-quoted markets over many thinly-watched ones.
```
