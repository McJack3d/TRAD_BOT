## 2026-06-08T08:58:49Z

You are the teamwork_preview_worker. Your task is to implement the risk safeguards for the Regime-Switching Long/Short Perp Bot inside `src/risk/perp_guards.py` and create unit tests in `tests/unit/test_perp_guards.py` with 100% code coverage.

### MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

### Context
Please read docs/REGIME_SWITCH_STRATEGY.md (especially §5 for stops and the kill-switch stack).

### Requirements
1. Implement `src/risk/perp_guards.py`. It must provide:
   - Configuration / parameter handling or helpers for:
     - `cooloff_bars` (default: 6)
     - `per_asset_daily_pct` (default: 0.015)
     - `max_consecutive_losses` (default: 4)
   - Validation functions/guards:
     - Cool-off check: Determine if an asset is under cool-off based on recent trades. If the last position close for the asset was a loss (stopped-out), block new entries for `cooloff_bars` on that asset.
     - Per-asset daily stop check: Determine if an asset's daily PnL (realized + unrealized) is <= -`per_asset_daily_pct` of the account equity. If so, pause trading for that asset for the rest of the UTC day.
     - Consecutive-loss breaker check: Determine if the last N trades (where N = `max_consecutive_losses`) were all losses. If so, halt all trading (set system status HALTED).
     - Account daily / cumulative stop checks: Wires daily loss <= -2% and cumulative loss <= -10% checks.
2. Implement unit tests in `tests/unit/test_perp_guards.py` validating every safeguard. Ensure coverage is 100% on `src/risk/perp_guards.py`.
3. Run the unit tests to verify implementation correctness and coverage.

Please write your progress and results to handoff.md in your working directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/worker_perp_guards/handoff.md. Include the commands used and output of test runs.
