# Regime-Switch Backtest Runbook

The strategy is built (`src/strategy/regime.py` + `regime_switch.py`),
the backtester applies the realistic cost model from the spec
(`src/backtest/regime_switch_backtest.py`), and a CLI drives both
(`scripts/backtest_regime_switch.py`). What's left is **running it
against real Binance perp history** and reading the scorecard against
the acceptance gates.

Binance's REST endpoints are geo-blocked from most cloud sandboxes, so
the backtest must run on a host with access — your Lightsail box in
Tokyo, which already has it.

---

## 1. Update the box (1 minute)

```bash
ssh ubuntu@18.178.6.237   # your static IP
cd ~/TRAD_BOT
git pull
source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. Run the quick smoke test (~30 seconds)

Sanity check that data downloads and the pipeline runs end-to-end:

```bash
python -m scripts.tradbot regime-quick
```

This pulls 2 months of BTC perp 1h candles (no funding model), runs
the strategy, prints a one-row scorecard, and tells you the gate
verdict. Useful first because:
- Download failure is the most common issue and you find out fast.
- 2 months is too short to hit the 100-trade gate, so "review" is the
  expected verdict — what matters is that it ran and the numbers look
  sane (Sharpe in a believable range, max DD < 50%).

## 3. Run the full backtest (~5–10 minutes)

The real test — BTC and ETH, 5m/15m/1h, 6 months, with the funding
cost model:

```bash
python -m scripts.tradbot regime-backtest
```

You get a scorecard like:

```
                 Regime-switch backtest scorecard
┏━━━━━━━━━┳━━━━┳━━━━━━┳━━━━┳━━━━━━━┳━━━━━━┳━━━━━┳━━━━━━━┳━━━━━┳━━━━━━━┓
┃ symbol  ┃ tf ┃trades┃win%┃Sharpe ┃max DD┃ APR ┃vs B&H ┃expo%┃ gate  ┃
┡━━━━━━━━━╇━━━━╇━━━━━━╇━━━━╇━━━━━━━╇━━━━━━╇━━━━━╇━━━━━━━╇━━━━━╇━━━━━━━┩
│ BTC/USDT│5m  │  ...                                            PASS │
│ BTC/USDT│15m │  ...                                          review │
│ ...
```

Plus the per-leg PnL attribution for the best-Sharpe row so you can see
whether the trend leg, the range leg, or both are carrying the result.

## 4. (Optional) Parameter sweep — see if defaults are near a plateau

```bash
python -m scripts.tradbot regime-sweep
```

Sweeps ADX threshold × ATR stop multiple on the 1h timeframe. The goal
is **plateau-finding**, not peak-finding — if one row has Sharpe 3.0
surrounded by Sharpe 0.5, that's overfit noise. Look for adjacent
cells with similar performance.

---

## 5. Read the scorecard against the gates

From `docs/REGIME_SWITCH_STRATEGY.md` §11:

| Gate | Threshold | Why it matters |
|---|---|---|
| Net Sharpe | > 1.0 | Without this, the edge is too small vs. its variance |
| Max drawdown | < 35% | Your stated risk tolerance |
| Trade count | ≥ 100 | Below this, results are noise |
| Profitable in trending AND ranging sub-period | — | Proves the regime switch earns its complexity |
| Walk-forward OOS Sharpe | > 0.5 | Robustness against parameter overfitting |

The CLI checks the first three; the last two need the per-period and
walk-forward breakdowns (next iteration's work — current results give
you the headline signal first).

---

## 6. Possible outcomes and what to do

- **Numbers look great across the board.** Be suspicious. Re-run with
  `--no-funding` to confirm funding isn't being modelled wrong, and
  check the per-leg attribution. If both legs contribute and exposure
  is moderate (~30–50%), proceed to walk-forward.
- **One leg is dead, one is alive.** Common, and actionable — disable
  the dead leg and re-test. Often the trend leg works on 1h+ and the
  range leg works on 5m, or vice versa.
- **Sharpe near zero or negative.** The strategy as-specified doesn't
  have an edge in the tested window. Don't tune until it does — that's
  overfitting. Options: try different timeframes, lengthen the test
  window, or shelve the strategy.
- **Network error / no data.** You're not on the Lightsail box, or the
  box is in a non-Tokyo region. Move it or change the network.

No matter the result, **nothing goes live without the gates passing,
walk-forward confirming, AND a clean 4-week paper run**. That's the
whole point of doing this in order.

---

## Reference: what each command actually does

```bash
# Direct equivalents (CLI under the hood is the same):
python -m scripts.backtest_regime_switch                                # default sweep
python -m scripts.backtest_regime_switch --symbols BTC/USDT --timeframes 1h --months 12
python -m scripts.backtest_regime_switch --sweep
python -m scripts.backtest_regime_switch --no-funding --refresh
```

`tradbot regime-*` is just a friendly menu wrapper. All knobs (months,
symbols, fees, leverage, risk%) live on the direct CLI.

Data is cached as Parquet under `data/history/`. Delete to force a
re-download, or pass `--refresh`.
