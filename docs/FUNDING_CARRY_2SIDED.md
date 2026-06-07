# Two-Sided Funding Carry — Strategy Spec

**Status:** design, pending sign-off. No code until approved.

**Goal:** extend the funding-arb daemon to harvest funding in **both**
directions — the existing positive-funding cash-and-carry, plus a new
negative-funding leg using Binance cross-margin to short spot.

---

## 0. Honest framing (read first)

This is an **incremental** improvement, not a paradigm shift. Reality:

- BTC/ETH funding is **positive ~75–85% of the time** (perp premium is
  the baseline). The existing one-sided bot already captures the bulk.
- Negative-funding windows are **shorter and shallower**, and the
  biggest ones cluster in bearish stress (May 2022, FTX) — exactly when
  borrow rates also spike.
- **The borrow cost is the strategy.** On the negative side you receive
  `|funding|` but pay margin interest. If the net doesn't clear the
  borrow rate, there is no trade.

Realistic uplift: **+10–30% more carry opportunity per year** after
borrow costs, concentrated in bear regimes. Not "double." Anyone who
tells you the negative side doubles your returns hasn't subtracted the
borrow rate.

The reason to do it anyway: it's the one strategy in this repo with a
**structural** (non-predictive) edge, it reuses the funding-arb
scaffolding and the just-fixed risk overlay, and the data to validate
it is fully available (confirmed below).

---

## 1. The carry math (the core of the spec)

Funding settles every 8h. There are 1095 settlements/year (3 × 365).

### Positive-funding leg (current behaviour)

```
position:   long spot  +  short perp   (delta-neutral)
income/8h:  + funding_rate × notional          (perp shorts receive +funding)
cost:       trading fees only (entry + exit)
net carry:  funding_rate                         per 8h
```
Enter when `funding_rate ≥ entry_funding_threshold` (today 0.0002 = 0.02%/8h).

### Negative-funding leg (new)

```
position:   long perp  +  short spot (borrowed via cross-margin)
income/8h:  + |funding_rate| × notional          (perp longs receive when funding < 0)
cost/8h:    borrow_rate_8h × notional            (interest on the borrowed spot)
            + trading fees (entry + exit)
            + borrow/repay is interest, no extra fee
net carry:  |funding_rate| − borrow_rate_8h      per 8h
```

where `borrow_rate_8h = borrow_rate_apr / 1095`.

**The entry rule is dynamic, not a fixed threshold:**
```
enter negative leg  ⇔  |funding_rate| − borrow_rate_8h ≥ entry_net_threshold
```
With `entry_net_threshold` defaulting to the same 0.0002/8h we use on
the positive side (so both legs demand the same *net* carry).

**Worked example.** Funding = −0.03%/8h looks juicy. But if BTC borrow
is 12% APR → borrow_8h = 0.12/1095 = 0.011%/8h. Net = 0.03 − 0.011 =
0.019%/8h ≈ 0.0002 — barely at threshold. At 20% APR borrow the same
funding nets **negative**. This is why the borrow rate must be read
live before every negative-leg entry.

### Exit rules (both legs)

- **Funding decays:** exit when `net carry ≤ exit_threshold` (hysteresis
  band below entry, as today: 0.00005/8h).
- **Min dwell:** respect `min_dwell_hours` (24h) so we don't churn fees
  on a single noisy settlement.
- **Borrow-rate spike (negative leg only):** if `borrow_rate_apr >
  max_borrow_rate_apr` (default 25%), the carry is gone — close.

---

## 2. New adapter surface (Binance cross-margin)

ccxt capabilities confirmed present on `ccxt.binance` (offline check):

| Need | ccxt method | Status |
|---|---|---|
| Borrow asset | `borrow_cross_margin(code, amount)` | ✅ `has[borrowCrossMargin]` |
| Repay asset | `repay_cross_margin(code, amount)` | ✅ `has[repayCrossMargin]` |
| Live borrow rate | `fetch_cross_borrow_rate(code)` | ✅ `has[fetchCrossBorrowRate]` |
| **Borrow-rate history** | `fetch_borrow_rate_history(code)` | ✅ `has[fetchBorrowRateHistory]` — **closes the backtest data gap** |
| Accrued interest | `fetch_borrow_interest()` | ✅ `has[fetchBorrowInterest]` |
| Margin balances | `fetch_balance({'type':'margin'})` | ✅ |
| Margin orders | `create_order(..., params={'marginMode':'cross'})` | ✅ (no dedicated createMarginOrder; unified create_order with margin params) |

New methods on `ExchangeAdapter` (and `BinanceAdapter` + `FakeExchange`):
```python
async def borrow(self, asset: str, amount: Decimal) -> None
async def repay(self, asset: str, amount: Decimal) -> None
async def fetch_borrow_rate(self, asset: str) -> Decimal          # APR
async def fetch_margin_balances(self) -> dict[str, Balance]       # incl. borrowed
async def fetch_borrow_interest(self, asset: str) -> Decimal      # accrued
async def submit_margin_order(self, symbol, side, qty, client_order_id) -> ExchangeOrder
```
`FakeExchange` gets simulated versions (configurable borrow rate, simple
interest accrual) so paper mode and tests exercise the full path.

---

## 3. State model changes

- `Position` gains `carry_side: Enum(POSITIVE, NEGATIVE)` so the exit
  logic and PnL attribution know which leg they're managing.
- `Position` gains `borrowed_asset` + `borrowed_amount` (negative leg).
- New `BorrowPayment` table (mirrors `FundingPayment`): per-interval
  accrued interest, so the daily/cumulative loss-stops and the
  `farb-status` monitor account for borrow cost in realized PnL.
- `build_state_snapshot` (already the source of truth for the loss
  stops) subtracts accrued borrow interest from realized PnL.

---

## 4. Risk overlay extensions

The negative leg adds two failure modes the current overlay doesn't cover:

1. **Spot-short liquidation.** A price *spike* can liquidate the
   margin short before the perp-long gain is realized. New pre-trade
   and continuous check: `margin_short_liq_distance_pct ≥
   pre_trade_min_liq_distance_pct` (reuse the 0.30 gate). Cross-margin
   liquidation distance comes from the margin account's margin level.
2. **Borrow-rate / availability risk.**
   - `max_borrow_rate_apr` (default 25%): refuse entry / close existing
     if breached.
   - **Borrow rejection:** Binance can run out of lendable inventory.
     The two-leg open must be **atomic-ish**: if the borrow fails,
     abort cleanly and never leave a naked perp long. Pre-flight the
     borrow before placing the perp leg; unwind the perp if the spot
     short can't be established within tolerance.

All new caps live in `RiskConfig` with conservative defaults and are
covered by adversarial tests (the existing risk suite pattern).

---

## 5. Data dependencies (all confirmed available)

- **Funding history:** `fetch_funding_rate_history` — already wired in
  `src/data/history.py`.
- **Borrow-rate history:** `fetch_borrow_rate_history` — confirmed
  present; add a loader alongside the funding loader, Parquet-cached.
- Both are needed for an honest backtest of the negative leg, since net
  carry = funding − borrow and we must replay both series.

---

## 6. Backtest plan & acceptance gates

There is **no working funding-arb backtest in the repo** today (the old
one imports a missing module). Build a fresh one reusing
`src/data/history.py`:

- Replay funding + borrow-rate series. For each 8h step, compute net
  carry per leg, simulate entries/exits with fees, dwell, and the
  dynamic borrow-adjusted threshold.
- **Attribute PnL by leg** (positive vs negative) so we see whether the
  negative leg actually contributes after borrow costs.

**Gates before paper (negative leg specifically):**
- Negative leg net-positive after borrow + fees over a window that
  *includes* a sustained negative-funding regime (e.g. mid-2022).
- Negative-leg Sharpe > 1.0 on its own contribution.
- ≥ 20 distinct negative-funding episodes traded (enough to not be a
  fluke — funding episodes are rarer than intraday bars).
- The combined (two-sided) equity curve has max DD ≤ the one-sided
  bot's max DD + 5pp (we're adding opportunity, not materially more
  risk).

If the negative leg can't clear borrow costs in the backtest, we ship
**positive-only** (no regression) and shelve the negative leg with a
post-mortem — same discipline as the regime build.

---

## 7. Build order (on sign-off)

1. Adapter surface: borrow/repay/rates/margin-balances/margin-order on
   `ExchangeAdapter` + `BinanceAdapter` + `FakeExchange` (+ tests).
2. Borrow-rate loader in `src/data/history.py` (+ tests).
3. Carry math module: pure `net_carry()` + entry/exit decision
   functions for both legs (+ tests). This is the heart — test it hard.
4. Backtester reusing the data loaders; per-leg attribution (+ tests).
5. **Run the backtest across 2021–2024 incl. the 2022 bear.** Check
   gates. STOP here if the negative leg fails.
6. Only if gates pass: state-model changes, risk-overlay extensions,
   execution wiring (atomic two-leg open with borrow pre-flight),
   `farb-status` display of borrow cost, config, paper deploy.

Steps 1–5 are the validation. Step 6 is gated on the numbers.

---

## 8. Open questions for sign-off

1. **`entry_net_threshold`** — same 0.02%/8h as the positive side, or
   higher to demand a borrow-cost safety margin? (Default: same.)
2. **`max_borrow_rate_apr`** — 25% reasonable, or tighter? Stress
   borrows have hit 50–100% APR briefly.
3. **Universe** — start negative leg on BTC + ETH only (deepest borrow
   liquidity), or include the daemon's full symbol list? (Default:
   BTC + ETH only for the negative leg; positive leg unchanged.)
4. **Isolated vs cross margin** — cross is simpler (one margin account,
   shared collateral) but a liquidation touches all margin positions;
   isolated quarantines risk per pair but needs per-pair transfers.
   (Default: cross, with a conservative margin-level gate.)

---

## 9. Decisions log (sign-off agreed 2026-06-09)

The proposed defaults were corrected by user review. Final agreed
parameters (overriding §1 / §4 / §8 above where they differ):

1. **Asymmetric thresholds.** The legs are not symmetric — the
   negative leg pays borrow, carries short-spot recall risk, and the
   regimes it activates in (negative funding) cluster with violent
   de-risking and short squeezes. Asymmetric risk → asymmetric demand.
   - Positive-side entry: `funding ≥ 0.0002` per 8h (unchanged).
   - **Negative-side entry: `|funding| − borrow_8h ≥ 0.0003`** per 8h
     (50% premium over positive side to compensate for the asymmetric
     downside). Net-of-borrow, NOT gross.
   - Both exits: `net_carry ≤ 0.00005` per 8h (hysteresis preserved).

2. **`max_borrow_rate_apr` tightened, AND netted into entry.**
   Original 25% was above the 0.02%/8h positive-side threshold
   (25%/1095 ≈ 0.023%/8h), which would have allowed trades where
   borrow cost alone exceeded the funding income. Two corrections:
   - **`max_borrow_rate_apr = 15`** (default 25). At 15% APR borrow is
     0.0137%/8h, safely below even the positive-side threshold.
   - **Entry decision uses net carry (income − borrow), never gross.**
     The strategy module's `enter_negative()` MUST take
     `current_borrow_rate` as an argument and reject if the net is
     below the threshold or if `current_borrow_rate ≥
     max_borrow_rate_apr`.
   - Continuous monitor closes any open negative-leg position if
     `current_borrow_rate ≥ max_borrow_rate_apr`.

3. **Universe per-leg.** Negative leg restricted to **BTC + ETH only**
   (deep borrow, low recall risk, tight basis). Positive leg keeps the
   existing config's symbol list. The strategy enforces this at the
   `evaluate_signal` boundary, not via config alone (defense in depth).

4. **Cross margin + account-level de-risk kill-switch.** Isolated
   margin would let one leg liquidate while the other survives,
   instantly converting a delta-neutral pair into a directional
   position — the precise risk we're trying to avoid. Cross keeps both
   legs alive on shared collateral. The cost is correlated cascade, so
   the risk overlay adds:
   - **`min_margin_level` continuous gate** — close ALL negative-leg
     positions when the cross-margin account's margin level drops
     below the configured floor (default 2.0; Binance liquidates
     around 1.1). Earlier than any single position's liquidation
     distance — global de-risk on margin-account stress.
   - **`max_total_borrow_notional_pct_equity`** — cap aggregate
     borrowed notional at e.g. 50% of equity so a single Margin-side
     spike can't cascade.

These changes are reflected in §1 (entry rules), §4 (risk overlay),
and the parameter defaults the strategy module will ship with. The
spec body above is left intact for the historical record; this section
is the authoritative version.

### Single-leg vs cross-venue caveat

The cross-margin choice assumes both legs sit on Binance's margin/perp
relationship. If a future deployment splits legs across venues, "cross"
loses meaning and we'd need per-venue buffers — flagged here so the
assumption is explicit.

---

## 10. Open questions remaining

None blocking. Build can proceed on this spec once (i) the AI sentiment
backtest has answered whether sentiment adds signal to the existing
trend bot (independent of this build, but the discipline applies), and
(ii) the regime-switch branch has been merged to main so the box is on
the hardened code first.
