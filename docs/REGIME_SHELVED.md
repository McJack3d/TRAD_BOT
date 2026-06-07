# Post-mortem: Regime-Switch Long/Short Perp Strategy — Shelved

**Status:** abandoned after empirical refutation. The infrastructure
(indicators, classifier, state machine, backtester, data loader,
diagnostic tooling) stays on `main` and is reusable. The directional
strategy itself does not.

---

## What we built (spec → code → evidence)

Followed `docs/REGIME_SWITCH_STRATEGY.md` end-to-end on
`claude/regime-switch-build`:

1. ATR / ADX / realized-vol / rolling-percentile indicators.
2. ADX-AND-vol regime classifier (TREND / RANGE / NEUTRAL).
3. State machine with two legs:
   - **TREND leg:** EMA(21/55) cross, long or short, ATR stop.
   - **RANGE leg:** Two-sided Bollinger + RSI mean reversion.
4. ATR-sized position with 3× leverage cap, 1% risk per trade.
5. Bar-by-bar backtester with realistic cost model: 4 bps taker each
   side, 2 bps slippage, 8h funding payments, cool-off after stops.
6. ccxt OHLCV + funding loader (Parquet-cached) and the diagnostic
   tooling that ultimately killed the project.

All on the Lightsail Tokyo box pulling real BTC/ETH perp data.

## What we observed

### Run 1: default parameters, 6 months BTC + ETH × 5m/15m/1h

| Cell | trades | Sharpe |
|---|---|---|
| BTC 5m  |  8 | +15.4 |
| BTC 15m | 16 | −1.2 |
| BTC 1h  | 14 | −3.9 |
| ETH 5m  | 14 | +5.4 |
| ETH 15m | 17 | −3.0 |
| ETH 1h  | 16 | −5.9 |

All cells failed the 100-trade gate. Best Sharpes (5m) were noise on
8–14 trades, with PnL of $73–84 over 6 months — rounding error.

### Diagnostic verdict

Built `regime-diagnose` to break the funnel down. Two findings:

**The range leg has a structural design flaw.** Across all 6 cells,
**86–92% of RANGE bars never touched the Bollinger bands at all.** The
reason is logically embedded: a RANGE regime is *defined* as low
realized vol, which means tight bands AND small swings within them.
Asking for a low-vol regime where the price still touches a 2σ band is
nearly contradictory. The range leg as designed will essentially never
fire on real crypto.

**The trend leg has healthy plumbing but no edge.** 87–92% enter-rate
inside TREND bars proved the EMA-alignment rule fires when it should.
The poor PnL is therefore not a sampling problem — it's the signal
itself losing money.

### Run 2 + 3: loosened regime gate (the smoking gun)

Lowered `adx_trend_min` 25→20 and `rv_high_pctile` 0.60→0.50 to roughly
double TREND occupancy and get into a statistically meaningful trade
count. Compared to Run 1:

| | Run 1 trades / Sharpe | Run 2 trades / Sharpe |
|---|---|---|
| BTC 5m  | 8 / +15.4 | 14 / +12.4 |
| BTC 15m | 16 / −1.2 | 15 / +2.1 |
| BTC 1h  | 14 / −3.9 | 21 / −1.8 |
| ETH 5m  | 14 / +5.4 | **27 / −16.0** |
| ETH 15m | 17 / −3.0 | 20 / −5.5 |
| ETH 1h  | 16 / −5.9 | **27 / −5.0** |

**ETH went from Sharpe +5.4 (on 14 trades, noise) to −16.0 (on 27
trades) at 5m, and from −5.9 to −5.0 at 1h with twice as many
trades.** A strategy with real edge tightens toward a positive mean as
you add samples. This one bled harder when allowed to trade more.

That's the textbook signature of negative expectancy.

Run 3 (loosened gate + range leg disabled) was nearly identical to
Run 2, confirming the range leg contributed nothing.

## Why this was the expected outcome

EMA-cross trend following on the two most-liquid, most-arbitraged
crypto pairs is the single most-tried retail strategy on earth. If it
worked easily, it wouldn't. The conservative ADX+vol gate was a
reasonable hypothesis — give the signal only the cleanest regimes to
trade — but the within-sample loosening result rules out the
"strategy works, sampling too thin" story.

For a small-account ($1k), high-frequency (5m–15m), high-fee setting
on the most-efficient corner of the market, this was always going to
be the modal outcome.

## What's reusable

Despite the strategy being shelved, the build leaves:

- `src/strategy/indicators.py` — ATR, ADX, realized-vol, rolling
  percentile. All Wilder-canonical, tested.
- `src/strategy/regime.py` — Regime classifier with both scalar (live)
  and vectorized (backtest) paths.
- `src/strategy/regime_switch.py` — A correct, tested state-machine
  pattern for two-legged strategies with ATR stops.
- `src/backtest/regime_switch_backtest.py` — Bar-by-bar perp
  backtester with the full cost model (fees + slippage + funding +
  ATR sizing + leverage cap + cool-off). Reusable for any
  long/short perp strategy.
- `src/backtest/regime_diagnostics.py` — The funnel diagnostic that
  killed this project. Any future regime-style strategy can use it.
- `src/data/history.py` — async + sync OHLCV + funding loader with
  Parquet cache.
- `scripts/backtest_regime_switch.py` — CLI with ablation knobs.

If you build a different long/short perp strategy, you'd plug a new
state machine in and reuse everything else. The negative result is
not a wasted build — it's the first strategy to be honestly refuted
by this infrastructure.

## What I'd consider next (in honest probability-of-edge order)

1. **Two-sided funding carry on the existing funding-arb scaffold.**
   The original audit flagged this as the one piece in the repo with
   structural (non-predictive) edge. Currently the daemon only
   harvests positive funding (long spot / short perp). Extending it
   to capture negative funding too — long perp / short spot when
   funding pays you to be long — could roughly double the
   opportunity window without changing the risk profile.

2. **Keep the existing daily trend bot (BTC SMA-200, spot only).**
   It's already proven, already running paper on the Lightsail box,
   already low-fee. Don't replace what works with something that
   doesn't.

3. **Funding-rate extreme mean-reversion.** When 8h funding hits the
   top 5% / bottom 5% of its rolling history, it tends to revert.
   Plausibly tradable from the funding-arb infrastructure with
   different entry/exit rules. Less crowded than EMA-cross.

4. **Walk away from intraday crypto for a $1k account.** Honest.
   Sub-$1k accounts on perps fight fees and minimums hard. The
   existing daily trend bot is the right shape of strategy for this
   account size.

I would **not** sweep the directional rule further. We already saw
that more trades make it worse — that's the signal.

## Decision log

- 2026-06-09 — Strategy shelved after Runs 1–3 confirmed negative
  marginal edge on the trend leg and structural failure of the range
  leg. Branch `claude/regime-switch-build` preserved but not merged
  to `main`. Infrastructure modules will be cherry-picked onto `main`
  once we know whether a future strategy uses them.
