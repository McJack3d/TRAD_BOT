# Analysis of Regime-Switching Long/Short Perp Bot implementation

This document reviews the codebase of the **Regime-Switching Long/Short Perp Bot** strategy, verifying the exact locations and implementation status of the required components (R1 to R4), as well as the test suite execution results.

---

## 1. Requirement Compliance Matrix

| Req | Description | Status | File Locations | Details / Notes |
|---|---|---|---|---|
| **R1** | **Core Strategy Logic & Classifier** | | | |
| | - Technical Indicators | **Completed** | `src/strategy/indicators.py` | Implements true range, ATR, ADX (and `plus_di`/`minus_di`), realized volatility, and rolling rank percentile rank. |
| | - Regime Classifier | **Completed** | `src/strategy/regime.py` | Implements `classify_regime` (scalar) and `classify_series` (vectorized) to categorize bars into `TREND`, `RANGE`, or `NEUTRAL` regimes. |
| | - Meta State Machine | **Completed** | `src/strategy/regime_switch.py` | Implements the meta-state machine (`FLAT -> ARMED -> LONG/SHORT -> FLAT`) managing the EMA-cross trend leg and Bollinger Bands mean-reversion leg. |
| | - ATR Sizing & Leverage | **Completed** | `src/backtest/regime_switch_backtest.py` (L138-144)<br>`src/strategy/regime_live.py` (L448-474) | Sizes positions dynamically using ATR to keep dollar risk-per-trade constant (default 1% of equity, 3x leverage ceiling). |
| **R2** | **Data Loader & Backtest Engine** | | | |
| | - Data Loader | **Completed** | `src/data/history.py` | Downloads OHLCV + funding history from Binance via CCXT, and caches them under `data/history/` as Parquet files. |
| | - Backtest Engine | **Completed** | `src/backtest/regime_switch_backtest.py` | Replays history bar-by-bar, netting taker fees (4 bps), slippage (2 bps), and funding payments (every 8h). |
| | - CLI Backtest Driver | **Completed** | `scripts/backtest_regime_switch.py` | Runs sweeps, walk-forward OOS periods, attributes P&L per regime, and prints scorecards against acceptance gates. |
| **R3** | **Live Execution & DB Integration** | | | |
| | - Execution Engine | **Completed** | `src/strategy/regime_live.py` | Coordinates live ticks, evaluates symbols via `evaluate_live`, checks stops intrabar/bar close, and handles position entry/exit. |
| | - Database Integration | **Completed** | `src/state/db.py` | Position states, orders, fills, and trade history are saved in SQLite database tables (Position, Order, Fill, SystemStatus, StateSnapshot). |
| | - Risk Guards | **Completed** | `src/risk/perp_guards.py` | Implements per-asset stops, consecutive-loss breaker, cool-off periods, and account-level daily loss-stops and cumulative loss-stops. |
| **R4** | **CLI Integration & Paper Trading** | | | |
| | - CLI Commands | **Completed** | `scripts/tradbot_regime.py`<br>`scripts/tradbot.py` | Implements the `regime-*` command group (`regime-backtest`, `regime-sweep`, `regime-quick`, `regime-diagnose`) registered in the main CLI. |
| | - Interactive Menu | **Completed** | `scripts/tradbot.py` (L1091-1147) | Option 4 in the interactive `tradbot menu` bot picker dispatcher is registered and maps to `_regime_menu`. |
| | - Paper Trading | **Completed** | `src/adapters/fake.py` | Leverages the in-memory mock exchange `FakeExchange` supporting balances, perp orders, tickers, funding rates, leverage and margins. |

---

## 2. Test Suite Status

We ran the entire test suite under `.venv/bin/pytest`. The results are as follows:

- **Total Tests executed**: 433
- **Passed**: 429
- **Skipped**: 3 (`tests/integration/test_binance_testnet.py` tests skipped due to missing API keys)
- **Failed**: 1 (`tests/unit/test_notifier_and_cron.py::test_status_when_not_installed`)

### Failure Diagnosis
The single failing test is:
`tests/unit/test_notifier_and_cron.py::test_status_when_not_installed`

**Root Cause**:
In `src/scheduler.py`, the `status()` function checks the existence of `p.plist`:
```python
def paths(project_root: Path) -> SchedulerPaths:
    home = Path.home()
    return SchedulerPaths(
        plist=home / "Library" / "LaunchAgents" / PLIST_NAME,
        ...
```
The plist path is hardcoded to be in the user's home folder `~/Library/LaunchAgents/com.tradbot.daily.plist` instead of relative to `project_root` or a sandbox temp folder.
Because the user `alexandrebredillot` already has `com.tradbot.daily.plist` installed in their real home folder on macOS, `s["installed"]` returns `True` instead of `False`, failing the test assertion `assert s["installed"] is False`.

### Strategy-Specific Tests
All 75 tests matching `tests/unit/test_regime*.py` and `tests/unit/test_perp_guards.py` **passed perfectly in 0.95s**.
This confirms that the strategy math, state transitions, classifier, backtest summaries, and perp risk guards are all fully verified and working as expected under unit tests.
