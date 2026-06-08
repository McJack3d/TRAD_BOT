# Handoff Report — worker_perp_guards

## 1. Observation
- Created the following files in the workspace:
  - `src/risk/perp_guards.py`: contains perp risk safeguards (cool-off check, per-asset daily stop, consecutive-loss breaker, and account daily/cumulative stops).
  - `tests/unit/test_perp_guards.py`: contains unit tests for all functions and configuration validations.
- Observed that executing shell commands failed due to `pytest` not being in the default global path (`zsh:1: command not found: pytest`) and timed out during the permission prompts for running `python3 -m pytest` in this non-interactive environment:
  > `Encountered error in step execution: Permission prompt for action 'command' on target 'python3 -m pytest tests/unit/test_perp_guards.py' timed out waiting for user response.`

## 2. Logic Chain
- **Risk Parameter & Configuration Defaults**:
  - Implemented `PerpRiskConfig` (Pydantic model) and `PerpRiskParams` (dataclass) to wrap `cooloff_bars` (default: 6), `per_asset_daily_pct` (default: 0.015), and `max_consecutive_losses` (default: 4) as per docs/REGIME_SWITCH_STRATEGY.md.
- **Cool-off Check**:
  - In `check_asset_cooloff`, we retrieve trades for the target asset. Trades are dynamically checked for fields (dictionary or object attributes) using `_get_field` helper.
  - The trades are sorted chronologically by `exit_bar_index` (primary) or `exit_ts` (secondary).
  - If the last trade was a loss (net_pnl < 0) and the elapsed bars since close (`current_bar_index - exit_bar_index`) is less than `cooloff_bars`, the new entry is blocked (returns `CheckResult.fail`).
- **Per-asset Daily Stop Check**:
  - In `check_asset_daily_stop`, if `asset_realized_pnl + asset_unrealized_pnl <= - (per_asset_daily_pct * account_equity)`, it blocks trading for the asset.
- **Consecutive-loss Breaker Check**:
  - In `check_consecutive_losses`, if the last `max_consecutive_losses` trades are all losses (sorted chronologically), it returns a failed `CheckResult`.
  - In `check_and_apply_consecutive_losses`, it wraps `check_consecutive_losses` and calls `await db.set_status(SystemStatusEnum.HALTED)` to halt all trading.
- **Account Daily / Cumulative Stop Checks**:
  - Wires `check_account_daily_stop` checking daily total pnl <= -2% of starting equity.
  - Wires `check_account_cumulative_stop` checking cumulative realized pnl <= -10% of starting equity.
- **100% Test Coverage Strategy**:
  - Imported `CheckResult` directly from `src.risk.checks` (since it is always present in the repository) rather than using a try-except fallback block, ensuring no lines are marked as uncovered.
  - Wrote test cases covering all configurations, helper functions, and all branches of validation logic (including missing parameters, empty list of trades, sorting by bar index vs timestamp, and mock DB interactions).

## 3. Caveats
- Since shell command execution timed out waiting for user permission approval, the tests could not be run directly during this run. The implementation was verified through extensive manual static code analysis.

## 4. Conclusion
- The risk safeguards inside `src/risk/perp_guards.py` are fully implemented according to the strategy spec, and the unit tests in `tests/unit/test_perp_guards.py` are complete, targeting 100% statement and branch coverage.

## 5. Verification Method
- To run the unit tests and verify 100% code coverage, run the following command in the workspace directory:
  ```bash
  python3 -m pytest tests/unit/test_perp_guards.py --cov=src.risk.perp_guards --cov-report=term-missing
  ```
- Alternatively, if using Poetry or virtualenv:
  ```bash
  poetry run pytest tests/unit/test_perp_guards.py --cov=src.risk.perp_guards --cov-report=term-missing
  ```
- Inspect `src/risk/perp_guards.py` and `tests/unit/test_perp_guards.py` directly to verify logical alignment.
