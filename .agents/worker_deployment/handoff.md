# Handoff Report — worker_deployment

## 1. Observation
- **File modified**: `src/strategy/regime_live.py` (lines 25-35, and lines 785-918).
  - Added CLI parser arguments: `--config` (default: `config/regime_switch.yaml`) and `--kill-file` (default: `/var/lib/bot/KILL`).
  - Added missing database model `FundingPayment` import on line 25:
    ```python
    from src.state.models import (
        Position,
        PositionStatus,
        Order,
        OrderStatus,
        Fill,
        SystemStatus,
        SystemStatusEnum,
        StateSnapshot,
        FundingPayment,
    )
    ```
  - Appended `run()`, `main()`, and `if __name__ == "__main__":` block.
- **File created**: `deploy/systemd/regime-bot.service`.
  - Added unit configuration mirroring `deploy/systemd/bot.service` but targeting `src.strategy.regime_live` strategy:
    ```ini
    [Unit]
    Description=trad-bot regime-switching long/short perp bot
    After=network-online.target chronyd.service
    Wants=network-online.target chronyd.service

    [Service]
    Type=simple
    User=botuser
    Group=botuser
    WorkingDirectory=/opt/trad-bot
    EnvironmentFile=/opt/trad-bot/.env
    ExecStart=/opt/trad-bot/.venv/bin/python -m src.strategy.regime_live --config /opt/trad-bot/config/regime_switch.yaml
    Restart=on-failure
    RestartSec=5s
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=trad-bot-regime
    ...
    ```
- **File modified**: `tests/unit/test_regime_live.py` (lines 538-575).
  - Added test cases `test_main_cli_argument_parsing()` and `test_main_cli_missing_config_exits()`.

## 2. Logic Chain
- To enable `src/strategy/regime_live.py` to run as a standalone daemon, it required a standard Python entrypoint mapping inputs to its internal `RegimeLiveBot` and run loops.
- `src/main.py` serves as the template for the main bot entrypoint. By mirroring its database, adapter, logging, and notifier initialization, we ensure `src/strategy/regime_live.py` has the identical operational capabilities.
- While reviewing `src/strategy/regime_live.py`, we observed that `FundingPayment` was being queried in `_get_asset_daily_realized_pnl` but was missing from imports. Thus, we added it to `from src.state.models import (...)`.
- To manage daemon execution lifecycle and signals (e.g., systemd stopping the service), we registered handlers for `SIGINT` and `SIGTERM` to gracefully stop `bot.run_loop()` and close all active DB/exchange resources.
- The unit test suite in `tests/unit/test_regime_live.py` has been updated with unit tests validating argparse handling and config existence checks.

## 3. Caveats
- No terminal execution of `pytest` was possible due to user prompt timeouts/permissions.
- Testing focused on modular/unit level verification. Integration-level testing will require a live exchange testnet connection.

## 4. Conclusion
- The `src/strategy/regime_live.py` script now supports standard CLI execution with configuration overrides and kill-file switches.
- The `deploy/systemd/regime-bot.service` matches all constraints and hardening settings of `bot.service`.

## 5. Verification Method
- Execute py_compile to ensure no syntax/compilation issues:
  `python3 -m py_compile src/strategy/regime_live.py`
- Run the python test suite to verify tests pass:
  `pytest tests/unit/test_regime_live.py`
