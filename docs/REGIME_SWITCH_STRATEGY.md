# Regime-Switching Long/Short Perp Bot — Strategy Spec

**Status:** design, pending sign-off. No code yet — this document is
the blueprint for the backtest phase.

**Owner:** Alexandre. **Scope:** a third Binance strategy (alongside the
funding-arb daemon and the SMA trend bot), trading BTC and ETH
perpetual futures intraday, long and short, with a regime switch.

---

## 0. Honest framing (read this first)

You chose the highest-variance corner of retail crypto: **aggressive +
intraday + leverage + a small (<$1k) account**. Every source surveyed
agrees this is where most retail money is lost — to fees, slippage, and
overfitting, not to bad luck. This spec does **not** pretend otherwise.
What it does is impose the discipline that gives the math a chance:

- **Leverage capped at 3×.** Not 10–20×. At 3× a routine 5% BTC move is
  a 15% equity swing, survivable; at 10× it's a liquidation.
- **1% of equity risked per trade**, sized off ATR — so volatile
  periods automatically get smaller positions.
- **Usually one position at a time** (the 1% risk budget rarely funds
  two full positions at $1k).
- **A fee floor you must clear:** ~12 bps round-trip (4 bps taker × 2 +
  ~2 bps slippage) + funding. At 5 trades/day on $1k that's
  ~$80–$200/month of friction the edge must beat *before* any profit.

**Design envelope (not a promise):** target 4–8% per month with a
25–35% peak-to-trough drawdown tolerance. Anything advertising 50%/month
is selling you something else. The **most likely outcome of an
unvalidated intraday strategy is a slow bleed to fees** — which is
exactly why nothing goes live until the backtest acceptance gates below
are met.

---

## 1. The idea in one paragraph

Markets alternate between **trending** and **ranging** regimes. A trend
strategy makes money in the former and bleeds in the latter; a
mean-reversion strategy does the opposite. Instead of betting on one,
classify the current regime each bar and run the matching leg:
**trend-following** (EMA cross, long or short) when the market is
trending, **mean-reversion** (Bollinger + RSI, long or short) when it's
ranging, and **stand aside** when the classifier is ambiguous. Size
every position off ATR so risk-per-trade is constant in dollars, and
wrap the whole thing in a layered kill-switch stack.

---

## 2. Regime classification (the core decision)

Computed per asset, per bar. You chose **ADX and realized-vol must both
agree** — the conservative option (fewer false flips, slower to adapt).

**Inputs**
- `ADX(adx_window)` — Wilder's Average Directional Index. Measures trend
  *strength* (not direction). Rising/high ADX = strong trend.
- `RV` = rolling standard deviation of log returns over `rv_window`,
  compared to its own rolling quantile over `rv_lookback`
  (`rv_percentile`).

**Rule**
```
TREND  regime  ⇔  ADX ≥ adx_trend_min      AND  RV ≥ rv_high_pctile
RANGE  regime  ⇔  ADX ≤ adx_range_max      AND  RV ≤ rv_low_pctile
NEUTRAL                          otherwise
```
- **TREND** → run the trend leg.
- **RANGE** → run the mean-reversion leg.
- **NEUTRAL** → no new entries; manage/close existing positions only.

Both indicators must agree because ADX lags and RV is noisy; requiring
consensus cuts whipsaw regime changes at the cost of entering a bit
later. (We can relax this to "either" in a later iteration if the
backtest shows it's too conservative.)

---

## 3. Signal legs

### 3a. Trend leg (TREND regime)

EMA cross with a close-confirmation:
```
LONG   ⇔  EMA(fast) > EMA(slow)  AND  close > EMA(slow)
SHORT  ⇔  EMA(fast) < EMA(slow)  AND  close < EMA(slow)
```
- **Entry:** on a fresh cross, or on entering the TREND regime already
  aligned.
- **Exit:** opposite cross, OR ATR stop hit, OR regime leaves TREND.

### 3b. Mean-reversion leg (RANGE regime)

Two-sided Bollinger + RSI (the BB-squeeze logic you already have,
mirrored to the short side):
```
LONG  setup  ⇔  close < lower_BB  AND  RSI < rsi_os   → trigger when close crosses back ABOVE lower_BB
SHORT setup  ⇔  close > upper_BB  AND  RSI > rsi_ob   → trigger when close crosses back BELOW upper_BB
```
- **Exit:** price reverts to mid-band (take-profit), OR ATR stop, OR
  regime leaves RANGE, OR setup expires (`setup_expiry_bars`).

### 3c. Meta-state machine

One state machine per asset, mirroring `src/strategy/bb_squeeze.py`:
`FLAT → ARMED → LONG/SHORT → FLAT`, with the regime gating which leg's
transitions are live. Pure function over bar history + prior state, so
it backtests identically to how it runs live.

---

## 4. Position sizing (ATR-based) and leverage

```
ATR            = ATR(atr_window)                       # in price units
stop_distance  = atr_mult * ATR                        # price units
risk_budget    = equity * risk_per_trade_pct           # e.g. 1% of equity
qty_base       = risk_budget / stop_distance           # base units (BTC/ETH)
notional       = qty_base * price
implied_lev    = notional / equity
# Hard cap: if implied_lev > max_leverage, scale qty down to the cap.
```
Consequence: when ATR is large (volatile), `qty_base` shrinks, so a stop
always costs ~`risk_budget`. The leverage cap (`max_leverage = 3`) is a
hard ceiling that can only *reduce* size.

---

## 5. Stops, exits, and the kill-switch stack

You chose **all** kill conditions. Layered from tightest to broadest:

| Layer | Trigger | Action | New? |
|---|---|---|---|
| Per-trade ATR stop | price hits entry ∓ `atr_mult·ATR` | close that position | new |
| Cool-off | after a stopped-out loss on an asset | blackout that asset for `cooloff_bars` | new |
| Per-asset daily stop | asset's daily PnL ≤ −`per_asset_daily_pct` | pause that asset for the rest of the UTC day | new |
| Consecutive-loss breaker | `max_consecutive_losses` losing trades in a row | **halt all**, manual resume | new |
| Account daily stop | total daily PnL ≤ −2% of start | **halt all** | exists (just fixed) |
| Account cumulative stop | total cumulative PnL ≤ −10% of start | **halt all** | exists (just fixed) |

- **Max concurrent positions:** 2 (one per asset), independent.
- **Trailing stop:** optional ATR ratchet in the profit direction
  (config flag; off by default until the backtest justifies it).

The two account-level stops reuse the machinery hardened in the previous
commit (real PnL snapshots), so they're already live and tested.

---

## 6. Entry filter — calendar + low-liquidity blackout

No **new** entries during:
- **Weekend low-liquidity window** — configurable UTC range
  (default Sat 00:00 → Sun 12:00). Crypto is 24/7 but weekend books are
  thin and gappy.
- **Scheduled macro events** — a small static config list of UTC
  datetimes (FOMC, CPI, major token unlocks) with a buffer
  (`event_buffer_before_min` / `_after_min`). Manually maintained for
  v1.

Exits and stops are **never** blocked by the filter — only new entries.

---

## 7. Timeframe — backtest decides

Run the identical logic on **5m, 15m, 1h** and select on net-of-cost
Sharpe with an adequate sample (≥100 trades). Lower timeframes mean more
trades and higher fee drag; the sweep tells us whether the edge survives
the friction at each scale. Document all three; don't cherry-pick.

---

## 8. Parameters (defaults + which to sweep)

| Param | Default | Sweep in backtest? |
|---|---|---|
| `adx_window` | 14 | maybe |
| `adx_trend_min` | 25 | **yes** |
| `adx_range_max` | 20 | **yes** |
| `rv_window` | 20 | maybe |
| `rv_lookback` | 200 | no |
| `rv_high_pctile` | 0.60 | **yes** |
| `rv_low_pctile` | 0.40 | **yes** |
| `ema_fast` / `ema_slow` | 21 / 55 | **yes** |
| `bb_window` / `bb_std` | 20 / 2.0 | maybe |
| `rsi_window` | 14 | no |
| `rsi_os` / `rsi_ob` | 30 / 70 | **yes** |
| `atr_window` | 14 | no |
| `atr_mult` (stop) | 2.0 | **yes** |
| `risk_per_trade_pct` | 0.01 | no (policy) |
| `max_leverage` | 3 | no (policy) |
| `cooloff_bars` | 6 | maybe |
| `per_asset_daily_pct` | 0.015 | maybe |
| `max_consecutive_losses` | 4 | maybe |
| `timeframe` | sweep | **yes (5m/15m/1h)** |

Guard against overfitting: sweep coarsely, prefer parameter *plateaus*
over sharp peaks, and confirm out-of-sample (§11).

---

## 9. Cost model (must be in the backtest)

- **Taker fee:** 4 bps per side (Binance USDT-M default; lower with BNB
  discount / VIP).
- **Slippage:** 2 bps per side assumed (market orders on BTC/ETH).
- **Funding:** every 8h (00/08/16 UTC) while a position is open — paid
  or received depending on side and the funding rate. Pull from funding
  history. Shorts *receive* positive funding (a small tailwind); longs
  pay it.

---

## 10. Where it fits in the codebase

```
src/strategy/
  indicators.py        EXTEND: add adx(), atr(), realized_vol(),
                       rolling_percentile() (bollinger/rsi/macd already here)
  regime.py            NEW: pure regime classifier (TREND/RANGE/NEUTRAL)
  regime_switch.py     NEW: meta state machine (long/short, ATR sizing)
src/risk/
  perp_guards.py       NEW: per-asset stop, consecutive-loss breaker,
                       cool-off, leverage cap (pure functions)
src/backtest/
  regime_switch_backtest.py   NEW: bar-by-bar sim (long/short + funding + fees)
src/data/
  history.py           NEW: OHLCV + funding loader via ccxt
                       (the src.data package is currently MISSING — see §13)
scripts/
  backtest_regime_switch.py   NEW: CLI to download data + run the sweep
config/
  regime_switch.yaml   NEW: the parameters above
tests/unit/
  test_regime.py, test_regime_switch.py, test_perp_guards.py,
  test_regime_switch_backtest.py   NEW
```
Reuses: `indicators.py`, the BB-squeeze state-machine pattern, the
`ExchangeAdapter`/`BinanceAdapter` perp methods (`set_leverage`,
`submit_order(reduce_only=…)`, `fetch_positions`), and the
just-fixed account-level loss-stops.

CLI/UI: add a `regime-*` command group + a 4th entry in the `tradbot`
bot-picker, mirroring the `farb-*` / `ibsent-*` pattern.

---

## 11. Backtest plan & acceptance gates

**Backtest deliverables**
1. Timeframe sweep (5m/15m/1h), net of the §9 cost model.
2. Per-regime PnL attribution (how much from the trend leg vs the
   range leg — if one leg is all the profit, simplify to just that leg).
3. Walk-forward: train on rolling windows, test out-of-sample.
4. Trade stats: win rate, avg win/loss, expectancy, max consecutive
   losses, max drawdown, exposure %.

**Gates before paper trading**
- Net-of-cost **Sharpe > 1.0** on the chosen timeframe.
- **Max drawdown < 35%** (your stated tolerance).
- **≥ 100 trades** over the test window (so it's not a fluke).
- **Profitable in BOTH** a clearly-trending sub-period AND a
  clearly-ranging sub-period (proves the switch earns its complexity).
- Walk-forward **OOS Sharpe > 0.5**.

**Gates before live (after the above + paper):**
- 4+ weeks paper on Binance testnet, behaviour within tolerance of the
  backtest.
- Every kill condition in §5 verified to fire in a simulated drawdown.
- Then micro-size live (the smallest notional Binance allows).

If the backtest fails the gates, we **simplify** (drop the weaker leg,
lengthen the timeframe) rather than push to live anyway.

---

## 12. Build order (once you sign off)

1. Indicators (`adx`, `atr`, `realized_vol`, `rolling_percentile`) + tests.
2. `regime.py` classifier + tests.
3. `regime_switch.py` meta state machine + tests.
4. `history.py` data loader (un-breaks `src.data`) + `backtest_regime_switch.py`.
5. Run the sweep, write up results, check gates.
6. Only if gates pass: `perp_guards.py`, live execution wiring, config,
   CLI/UI, paper deployment.

Steps 1–5 are the "backtest in a second time" you referenced. Step 6 is
gated on the numbers.

---

## 13. Known prerequisite / risk

- The `src/data` package (`market_data`, `historical`) is **missing**
  from the repo — the funding-arb daemon entrypoint and 5 tests can't
  import it. The backtest needs an OHLCV+funding loader anyway, so step 4
  above rebuilds the history side. The live market-data stream (WS) for
  this strategy is a separate, later concern.
- Overfitting is the dominant risk at this frequency. The coarse-sweep +
  plateau + walk-forward discipline in §8/§11 is the main mitigation.
- Regime classifiers lag. Expect to give back some profit at regime
  turns; that's the cost of not being whipsawed.

---

*Sign-off needed on:* the regime rule (§2), the two legs (§3), the
sizing/leverage policy (§4), and the acceptance gates (§11). Once you're
happy, I'll start at §12 step 1.
