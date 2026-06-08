# Original User Request

## 2026-06-08T08:54:54Z

Implement the Regime-Switching Long/Short Perp Bot (live execution engine) on Binance as a new strategy daemon, trading BTC and ETH perpetual futures intraday according to the specification in docs/REGIME_SWITCH_STRATEGY.md.

Working directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT
Integrity mode: benchmark

## Requirements

### R1. Live Strategy Execution Daemon
Build the live evaluation path (`evaluate_live`) for the regime-switching strategy (`src/strategy/regime_switch.py`). The daemon must run continuously, evaluate signals on bar close (5m/15m/1h based on config), and submit trades accordingly.

### R2. Systemd Service & Execution Modes
Package the strategy as a systemd service (`regime-bot.service`) supporting `DRY_RUN` (simulate execution on live mainnet data), `PAPER` (fake exchange execution), and `LIVE` (real trade execution) modes.

### R3. Safeguards & Kill-Switch Stack
Implement `src/risk/perp_guards.py` providing the checks in §5 (ATR stops, cool-offs, consecutive-loss breakers, daily stops, and account-level limits). The bot must halt trading and set status to HALTED if any limit is breached.

### R4. Unified Database Persistence
Share the existing database (`data/bot.db`) and reuse standard database tables/models for recording position entries/exits, orders, and daily snapshots.

## Acceptance Criteria

### Automated Validation
- [ ] Code passes ruff linting and mypy type checks.
- [ ] Unit tests implemented in `tests/unit/test_perp_guards.py` cover all limits with 100% test coverage.
- [ ] Unit tests implemented in `tests/unit/test_regime_live.py` validate the live tick/state loop with `FakeExchange`.
- [ ] All 400+ unit/integration tests pass on the VPS test suite.

### CLI & UI Integration
- [ ] `scripts/tradbot.py` is extended to support `regime-*` subcommands displaying positions, metrics, and logs.

### Service Deployment
- [ ] `regime-bot.service` file is installed in `/etc/systemd/system/` and runs in `DRY_RUN` mode on the VPS without errors.
