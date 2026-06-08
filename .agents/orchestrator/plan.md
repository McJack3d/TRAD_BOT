# Project Plan: Regime-Switching Long/Short Perp Bot implementation

## Milestones

### Milestone 1: Risk Safeguards (`src/risk/perp_guards.py` & `tests/unit/test_perp_guards.py`)
- **Description**: Implement risk safeguards in `src/risk/perp_guards.py` covering ATR stops, cool-offs, consecutive-loss breakers, daily stops, and account-level limits. Implement unit tests in `tests/unit/test_perp_guards.py` covering all limits with 100% coverage.
- **Verification**: Run `pytest tests/unit/test_perp_guards.py` and verify all tests pass with 100% coverage.
- **Status**: COMPLETED

### Milestone 2: Live Strategy Daemon & Loop (`src/strategy/regime_live.py` & `tests/unit/test_regime_live.py`)
- **Description**: Implement the continuous live evaluation path daemon/loop that evaluates on bar close (5m/15m/1h), checks risk limits from Milestone 1, and submits orders to CCXT. Wires `evaluate_live` in `src/strategy/regime_switch.py`. Wires database persistence sharing `data/bot.db` and using standard tables.
- **Verification**: Implement unit tests in `tests/unit/test_regime_live.py` using `FakeExchange` to validate the live tick and state loop. Run `pytest tests/unit/test_regime_live.py` and ensure they pass.
- **Status**: COMPLETED

### Milestone 3: CLI Subcommands Expansion (`scripts/tradbot.py` & `scripts/tradbot_regime.py`)
- **Description**: Extend `scripts/tradbot.py` and `scripts/tradbot_regime.py` to support `regime-*` subcommands displaying positions, metrics, and logs for the live execution bot.
- **Verification**: Run `python -m scripts.tradbot regime-status`, etc., and verify the output displays correctly.
- **Status**: COMPLETED

### Milestone 4: Systemd Service Deployment (`deploy/regime-bot.service`)
- **Description**: Create and package the systemd service file `regime-bot.service` configured to run in DRY_RUN, PAPER, or LIVE mode. Provide instructions to install it in `/etc/systemd/system/` and run it in `DRY_RUN` mode.
- **Verification**: Service runs on VPS in `DRY_RUN` mode without errors.
- **Status**: COMPLETED

## Interface Contracts
- **Live Strategy Daemon (`regime_live.py`) ↔ Risk Guards (`perp_guards.py`)**:
  - `perp_guards.py` exposes state check functions and breakers that accept position, balance, and historical metrics to determine if new entries or holding positions are allowed.
- **Live Strategy Daemon (`regime_live.py`) ↔ Database (`src/state/db.py`)**:
  - Uses standard sqlite DB models, representing long/short perpetual positions as `Position` records with `spot_qty=0`.
