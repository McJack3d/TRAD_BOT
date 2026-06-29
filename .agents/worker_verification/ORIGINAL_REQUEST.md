## 2026-06-25T18:33:12Z
You are the Verification and QA Worker.
Objective:
1. Fix the failing test `test_status_when_not_installed` in `tests/unit/test_notifier_and_cron.py`. Use pytest's `monkeypatch` fixture to mock `src.scheduler.Path.home` or `src.scheduler.paths` to target the temporary directory `tmp_path`, so it doesn't fail if the launchd plist is already installed in the user's real home folder.
2. Run the full pytest suite under `.venv/bin/pytest` and verify that 100% of the tests pass.
3. Run the backtesting CLI command `python -m scripts.tradbot regime-backtest` or `python scripts/backtest_regime_switch.py` to verify the backtest runs and outputs the required statistics (Sharpe, Max Drawdown, etc.). Verify that the outputs meet the acceptance criteria: Sharpe > 1.0, Max drawdown < 35%, and at least 100 trades.
4. Report the fix description, the final pytest results, and the backtest scorecard output.
Working Directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/worker_verification/
Workspace: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
