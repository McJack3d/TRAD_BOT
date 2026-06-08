## 2026-06-08T14:01:28Z

You are the teamwork_preview_worker. Your task is to extend the CLI subcommands in `scripts/tradbot_regime.py` to support live bot operations for the Regime-Switching Long/Short Perp Bot (Milestone 3).

### MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

### Context
Please read docs/REGIME_SWITCH_STRATEGY.md, scripts/tradbot_regime.py, scripts/tradbot_farb.py, and src/strategy/regime_live.py.

### Requirements
Extend `scripts/tradbot_regime.py` to add subparsers and handlers for the following subcommands (all prefixed with `regime-`):
1. `regime-status`:
   - Read from the YAML config (e.g. `config/regime_switch.yaml`) and database (`data/bot.db`).
   - If database exists, fetch current system status (Active, Paused, Halted, and reason).
   - Display a clean summary of starting equity, open positions (symbol, contracts/size, side, entry price, stop price, initial margin, unrealized PnL), snapshot age, daily/cumulative realized PnL, daily stop headroom, cumulative stop headroom, and consecutive losses count.
2. `regime-positions`:
   - List all open perp positions stored in DB (reusing SQLAlchemy model `Position` where status is OPEN).
3. `regime-equity`:
   - Show a historical table of recent equity snapshots (using `StateSnapshot` from DB).
4. `regime-enable`:
   - Mark the bot as enabled (`enabled="true"` in SystemStatus.halt_reason metadata).
5. `regime-disable`:
   - Mark the bot as disabled (`enabled="false"` in SystemStatus.halt_reason metadata).
6. `regime-evaluate`:
   - Initialize the exchange client, database, and RegimeLiveBot.
   - Run one single signal evaluation tick (`await bot.tick()`) immediately.
7. `regime-flatten`:
   - Force close all open perp positions (using opposing market orders with reduce_only=True) and mark positions as closed in the DB.
8. Update the interactive sub-menu or menu in `scripts/tradbot_regime.py` to expose these new options when run interactively.

Write your findings, implemented design, and test runs to `/Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/worker_cli_subcommands/handoff.md`.
