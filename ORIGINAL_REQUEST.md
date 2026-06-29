# Original User Request

## Initial Request — 2026-06-25T18:28:23Z

Implement the **Regime-Switching Long/Short Perp Bot** strategy in the `TRAD_BOT` repository, including the indicators, classifier, state machine, backtester, database integration, risk guards, and CLI commands.

Working directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT
Integrity mode: development

## Requirements

### R1. Core Strategy Logic & Classifier
- **Indicators**: Extend `src/strategy/indicators.py` to add ADX, ATR, realized volatility, and rolling percentile.
- **Regime Classifier** (`src/strategy/regime.py`): Pure classifier identifying `TREND`, `RANGE`, or `NEUTRAL` regimes based on ADX and realized volatility.
- **Meta State Machine** (`src/strategy/regime_switch.py`): Logic gating the EMA-cross trend leg (`TREND` regime) and Bollinger Squeeze mean-reversion leg (`RANGE` regime). Fits a clean state machine: `FLAT -> ARMED -> LONG/SHORT -> FLAT`.
- **ATR Sizing**: Size positions dynamically based on ATR so the dollar-risk-per-trade is constant (risk default `1%` of equity, leverage cap at 3x).

### R2. Data Loader & Backtest Engine
- **Data Loader** (`src/data/history.py`): Load OHLCV + funding history from Binance via CCXT or Parquet cache.
- **Backtester** (`src/backtest/regime_switch_backtest.py` and `scripts/backtest_regime_switch.py`):
  - Simulates the strategy bar-by-bar (5m, 15m, 1h).
  - Integrates the fee model (4 bps taker + 2 bps slippage per side) and funding payments (every 8h).
  - Generates rolling walk-forward metrics (6-month train / 1-month test) and attributes P&L by regime.

### R3. Live Execution Wiring & Database Integration
- **Execution Engine**: Coordinate two-leg execution (spot buy + perp short for entries, or spot sell + perp buy for unwinds) with isolated margin and leverage limits.
- **Database Integration**: Save position states, orders, fills, and trade histories to the existing SQLite database schema in `src/state/db.py` (adding new fields/tables if needed for carry sides or risk trackers).
- **Risk Guards**: Implement per-asset stops, consecutive-loss breaker, cool-off periods, leverage caps, and verify they integrate with the account-level loss-stops.

### R4. CLI Integration & Paper Trading
- **CLI Commands**: Add a `regime-*` command group (e.g. `regime-status`, `regime-tick`, `regime-watch`) to `scripts/tradbot.py` and update the interactive `tradbot menu` bot picker.
- **Paper Trading**: Ensure the bot can run against the existing `FakeExchange` mock environment.

## Verification Mechanisms

- **Unit and Integration Tests**: Write and execute dedicated tests (`tests/unit/test_regime.py`, `tests/unit/test_regime_switch.py`, `tests/unit/test_perp_guards.py`, `tests/unit/test_regime_switch_backtest.py`). 100% of these tests must pass.
- **Programmatic Backtest Output**: Executing the backtesting script must successfully output trade statistics, drawdowns, and Sharpe ratios.

## Acceptance Criteria

### Technical & Code Quality
- [ ] 100% of unit tests pass under `.venv/bin/pytest`.
- [ ] No syntax, import, or type-checking errors in the newly added files.
- [ ] Database schema is updated without breaking existing Binance funding-rate or BTC Trend bots.

### Backtest Performance
- [ ] Combined backtest over historical data shows net-of-cost Sharpe > 1.0 on the selected timeframe.
- [ ] Max drawdown is strictly less than 35%.
- [ ] Reaches at least 100 trades over the test window.
- [ ] Strategy is profitable in both a clearly-trending sub-period and a clearly-ranging sub-period.
- [ ] Walk-forward out-of-sample (OOS) Sharpe > 0.5.

### Runtime Operations
- [ ] Running `tradbot menu` shows the new "Regime-Switching Perp Bot" as option 4.
- [ ] CLI command `tradbot regime-tick` evaluates the regime classifier, fetches prices, and executes mock orders in paper mode successfully.
- [ ] Risk guards successfully trigger a halt and flatten positions in a simulated drawdown.
