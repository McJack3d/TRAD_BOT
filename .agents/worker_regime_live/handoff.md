# Handoff Report - worker_regime_live

## 1. Observation

- Created the live execution engine daemon in `src/strategy/regime_live.py`.
- Created comprehensive unit tests in `tests/unit/test_regime_live.py` utilizing the `FakeExchange` adapter and mocks.
- Created standard configuration file in `config/regime_switch.yaml`.
- The following files were inspected for context:
  - `docs/REGIME_SWITCH_STRATEGY.md`
  - `src/strategy/regime_switch.py`
  - `src/risk/perp_guards.py`
  - `src/simple_bot.py`
- Executed `run_command` targeting `python3 -m pytest tests/unit/test_regime_live.py` which timed out waiting for user approval:
  > `Encountered error in step execution: Permission prompt for action 'command' on target ... timed out waiting for user response.`

## 2. Logic Chain

1. **Strategy Alignment**: From `docs/REGIME_SWITCH_STRATEGY.md` and `src/strategy/regime_switch.py`, we observed that the strategy requires classifying the market regime (`TREND`, `RANGE`, `NEUTRAL`) and executing long/short trades with ATR-based position sizing and leverage caps.
2. **Bot Design**: We modeled `RegimeLiveBot` after `SimpleBot` (`src/simple_bot.py`), using the same database session/update pattern and status properties (`is_enabled`, `is_halted`, `enable`, `disable`).
3. **Sizing Logic**: Position sizing uses `risk_budget = equity * risk_per_trade_pct` and `stop_distance = atr_mult * ATR`. The size `qty` is capped at `max_leverage` (default: 3.0), rounded to `qty_step`, and validated against `min_qty`.
4. **Breakers & guards**:
   - `check_asset_cooloff` is run before entry, with `exit_bar_index` dynamically matched or loaded from meta properties.
   - `check_asset_daily_stop` pauses the specific asset if it hits daily loss limits.
   - `check_consecutive_losses`, `check_account_daily_stop`, and `check_account_cumulative_stop` act as breakers that halt the entire bot (`SystemStatusEnum.HALTED`) and close all open perp positions.
5. **Testing Strategy**: We implemented unit tests in `tests/unit/test_regime_live.py` covering all of these logic branches. Mocks are used to trigger specific `evaluate_live` signals while `FakeExchange` handles order fills, balance changes, and pricing updates.

## 3. Caveats

- Asynchronous command execution failed due to macOS command approval timeouts.
- Out-of-bounds/external networking is blocked, meaning real live CCXT connection was simulated via `FakeExchange` and dataframes.

## 4. Conclusion

The Live Execution Daemon `RegimeLiveBot` and its test suite `test_regime_live.py` are fully implemented, structurally compliant with the project conventions, and ready for validation.

## 5. Verification Method

To verify the implementation and run the unit tests, execute the following command in the terminal:

```bash
python3 -m pytest tests/unit/test_regime_live.py
```

### Files to Inspect
- `src/strategy/regime_live.py` — Engine class and loop logic.
- `tests/unit/test_regime_live.py` — Unit tests.
- `config/regime_switch.yaml` — Strategy default parameters.
