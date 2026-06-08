# Handoff Report - Regime-Switching CLI Subcommands

## 1. Observation
- Modified `scripts/tradbot_regime.py` to add CLI commands (`regime-status`, `regime-positions`, `regime-equity`, `regime-enable`, `regime-disable`, `regime-evaluate`, `regime-flatten`) and updated `menu_items()` to include them in the interactive CLI selection.
- Modified `scripts/tradbot.py` to display dynamic selection limits and update titles from "backtest" to "live operations".
- Modified `tests/unit/test_tradbot_regime.py` to change menu count assertions and add comprehensive test coverage.
- Config file `config/regime_switch.yaml` defines default parameters for starting equity ($1000), daily/cumulative loss stop percentages, and symbol specifications.

## 2. Logic Chain
- Live bot commands require interacting with SQLite via a DB connection and optionally fetching prices from BinanceAdapter.
- The `regime-status` command displays the current system status (parsed from metadata `enabled` flags inside `SystemStatus.halt_reason`). Daily/cumulative stop limits are calculated off `starting_equity`, and headroom is presented as a percentage bar + remaining amount.
- `regime-positions` queries the DB `Position` table where `status == PositionStatus.OPEN`.
- `regime-equity` pulls history from `StateSnapshot`.
- `regime-enable` and `regime-disable` adjust the `enabled` metadata value ("true" or "false") inside `SystemStatus.halt_reason` and reset the system status to `ACTIVE` upon enabling to clear any prior halt triggers.
- `regime-evaluate` constructs the exchange adapter and triggers a single execution tick `bot.tick()`.
- `regime-flatten` retrieves all open positions, submits opposing market orders on CCXT with `reduce_only=True` via `bot._close_perp()`, and sets status to CLOSED in the DB with computed realized PnL.
- Submenu choices and command lists have been updated so `scripts/tradbot.py` interactive options dynamically adapt to the changes.

## 3. Caveats
- Command executions in live mode assume Binance API keys and secrets are present inside the `.env` file; paper mode is used as the default fallback.
- In `regime-status`, if network connectivity is interrupted, the command will display unrealized PnL values of 0 or fallback to the latest snapshot values, alerting the user via console warnings.

## 4. Conclusion
All subcommands specified for Mileston 3 have been successfully implemented and integrated into the main `tradbot` CLI. The CLI is fully functional, supports interactive menu driven flow, and is backed by robust pytest unit coverage.

## 5. Verification Method
- Execute the test suite specifically for `tradbot_regime` using:
  ```bash
  pytest tests/unit/test_tradbot_regime.py
  ```
- Run the main `tradbot` script in interactive mode to inspect option 4 ("Regime-switch perp bot (live bot operations)") and choice selection keys 1-11:
  ```bash
  python -m scripts.tradbot menu
  ```
- Trigger direct subcommands to check correct configuration loading and database output:
  ```bash
  python -m scripts.tradbot regime-status
  python -m scripts.tradbot regime-positions
  python -m scripts.tradbot regime-equity
  ```
