# Project: Regime-Switching Long/Short Perp Bot

## Architecture
- Core Indicators & Classifier: ADX, ATR, Realized Volatility, Rolling Percentile. Regime Classifier outputs TREND, RANGE, or NEUTRAL.
- Meta State Machine: Gates EMA-cross trend leg and Bollinger Squeeze mean-reversion leg. ATR-based sizing.
- Data Loader & Backtest: SIM/Historical CCXT, walk-forward, fee model + funding payments.
- Live Execution & DB: Coordinate spot/perp order execution, isolated margin, sqlite DB positions/fills/orders, risk guards.
- CLI: `regime-*` command group and menu option 4.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Core Strategy Verification | Indicators, regime classifier, state machine, and tests. | none | DONE |
| M2 | Backtesting Suite | Data loading, backtest engine, metric computation, Sharpe/DD limits. | M1 | DONE |
| M3 | Live Execution & Risk Guards | Execution engine, database tracking, per-asset and account stops. | M1 | IN_PROGRESS |
| M4 | CLI & Paper Trading | CLI commands, menu integration, FakeExchange trading. | M3 | PLANNED |
| M5 | E2E Testing & Coverage Hardening | Opaque-box E2E test suite, white-box coverage hardening, forensic audit. | M4 | PLANNED |

## Code Layout
- `src/strategy/indicators.py`: Technical indicators
- `src/strategy/regime.py`: Regime classifier logic
- `src/strategy/regime_switch.py`: Meta state machine and signal evaluation
- `src/strategy/regime_live.py`: Daemon for live/paper execution
- `src/risk/perp_guards.py`: Risk guards
- `src/data/history.py` / `src/data/historical.py`: Data loading
- `src/backtest/regime_switch_backtest.py`: Backtester
- `scripts/tradbot.py` / `scripts/backtest_regime_switch.py`: CLI scripts
- `tests/unit/`: Unit tests
- `tests/integration/`: Integration and paper trading tests
