# Handoff Report — Codebase Investigator explorer

## 1. Observation
- Verified that all components of the **Regime-Switching Long/Short Perp Bot** are implemented across the following files:
  - **Technical Indicators**: `src/strategy/indicators.py` (Lines 90-175: `true_range`, `atr`, `adx`, `realized_vol`, and `rolling_rank_pct`)
  - **Regime Classifier**: `src/strategy/regime.py` (Lines 1-105: `classify_regime` and `classify_series`)
  - **Meta State Machine**: `src/strategy/regime_switch.py` (Lines 1-284: `evaluate_at`, `evaluate_live`, `SwitchPosition`, `SwitchSignal`)
  - **Data Loader**: `src/data/history.py` (Lines 1-381: downloads and caches candles/funding history)
  - **Backtester**: `src/backtest/regime_switch_backtest.py` (Lines 1-262: bar-by-bar sim with fees and funding, plus ATR-based sizing at lines 138-144) and `scripts/backtest_regime_switch.py` (Lines 1-364: sweep and acceptance gates scoring)
  - **Live Execution Daemon**: `src/strategy/regime_live.py` (Lines 1-774: `RegimeLiveBot` coordinates live evaluation ticks, checks stops, checks risk guards, sizes orders, and updates DB metadata)
  - **Risk Guards**: `src/risk/perp_guards.py` (Lines 1-233: `check_asset_cooloff`, `check_asset_daily_stop`, `check_consecutive_losses`, `check_account_daily_stop`, and `check_account_cumulative_stop`)
  - **Database Integration**: `src/state/db.py` (Lines 1-265: CRUD and queries on SQLite models)
  - **CLI Commands**: `scripts/tradbot_regime.py` (Lines 1-130: `regime-*` subcommands) and `scripts/tradbot.py` (Lines 1091-1147: `tradbot menu` Option 4 registrations)
  - **Paper Trading**: `src/adapters/fake.py` (Lines 1-385: `FakeExchange` mock exchange adapter)
- Ran the full test suite via `.venv/bin/pytest`. Verbatim result:
  `FAILED tests/unit/test_notifier_and_cron.py::test_status_when_not_installed`
  `1 failed, 429 passed, 3 skipped, 1 warning in 130.55s (0:02:10)`
- Observed that the single failure occurs at:
  ```python
  tests/unit/test_notifier_and_cron.py:109: AssertionError
  >           assert s["installed"] is False
  E           assert True is False
  ```
- Checked the implementation of `status()` in `src/scheduler.py` (Line 163-165):
  ```python
  p = paths(project_root)
  if not p.plist.exists():
      return {"platform": "darwin", "installed": False}
  ```
  Where `paths(project_root)` (Line 38) always returns:
  `plist=home / "Library" / "LaunchAgents" / PLIST_NAME`
  Which checks for a plist in the real user's home directory.
- Ran specific unit tests related to regime-switching and risk guards via:
  `.venv/bin/pytest tests/unit/test_regime*.py tests/unit/test_perp_guards.py`
  Result:
  `75 passed in 0.95s`

---

## 2. Logic Chain
1. **Indicator, Classifier, and State Machine Presence**: Directly reading `src/strategy/indicators.py`, `src/strategy/regime.py`, and `src/strategy/regime_switch.py` confirms that all core logic for R1 is implemented.
2. **Backtester and Sizing**: Examining `src/backtest/regime_switch_backtest.py` and `src/strategy/regime_live.py` shows that the ATR-based stop distance sizing (risking 1% equity, capped by max leverage of 3x) is correctly coded and matched to the spec.
3. **Data Loader & CLI**: Inspecting `src/data/history.py`, `scripts/backtest_regime_switch.py`, and `scripts/tradbot_regime.py` confirms R2 and R4. The `tradbot menu` registers the strategy as choice 4.
4. **Live Execution Wiring & Risk Guards**: Viewing `src/strategy/regime_live.py` and `src/risk/perp_guards.py` confirms that R3 is fully realized. `RegimeLiveBot` coordinates live ticks, stops checking, asset daily stops, consecutive losses halts, and cool-off periods.
5. **Test Suite Outcomes**: The full test suite execution shows that 429 out of 430 unit tests pass.
6. **Assertion Failure Cause**: The single failure (`test_status_when_not_installed`) checks `status(tmp_path)` and asserts `s["installed"]` is `False`. However, since `status` always checks `~/Library/LaunchAgents/com.tradbot.daily.plist` (which exists on the current user's system), it returns `True`, failing the assertion.
7. **Regime Switching Verification**: Since all 75 regime-switching and perp risk guard unit tests (`test_regime.py`, `test_regime_switch.py`, `test_perp_guards.py`, `test_regime_switch_backtest.py`, etc.) pass, the regime-switching implementation itself is verified.

---

## 3. Caveats
- **Live Trading Connectivity**: The live CCXT exchange endpoints have not been tested with real credentials because the sandbox runs in CODE_ONLY mode, and API keys are not supplied. Live trading must only be conducted after a successful paper trading run.
- **Weekend Blackout & Macro Buffer**: Weekend and macro buffers are implemented but depend on correct system UTC time synchronization.

---

## 4. Conclusion
The **Regime-Switching Long/Short Perp Bot** is fully implemented according to Requirements R1 through R4. The codebase is complete, containing all indicators, regime classification, meta-state machinery, data loaders, backtesters, live execution tick loops, risk guards, SQLite integration, and CLI/interactive menu wiring.

Apart from a single launchd test assertion failing due to an existing plist on the user's local Mac system, **all 429 other unit tests pass successfully**. Crucially, **100% of the 75 regime-switching-related tests pass**.

---

## 5. Verification Method
To independently verify the test suite status and regime-switching logic:
1. Run all unit tests:
   ```bash
   .venv/bin/pytest tests/unit/
   ```
   *Expected outcome*: 429 passed, 1 failed (due to the `test_status_when_not_installed` macOS environment clash).
2. Run regime-specific tests:
   ```bash
   .venv/bin/pytest tests/unit/test_regime*.py tests/unit/test_perp_guards.py
   ```
   *Expected outcome*: `75 passed`.
3. Verify the CLI menu:
   ```bash
   python -m scripts.tradbot menu
   ```
   *Expected outcome*: Top-level picker displays option 4: `Regime-switch perp bot (backtests — pre-deployment)`.
