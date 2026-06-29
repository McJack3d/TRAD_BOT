## 2026-06-25T20:34:37Z

You are worker_verification_2, a teamwork_preview_worker.
Your working directory is: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/worker_verification_2/

Please read your BRIEFING.md, progress.md, and ORIGINAL_REQUEST.md in your working directory for detailed task definitions.

Tasks:
1. Run python pytest via `.venv/bin/pytest` to confirm the failure of `test_status_when_not_installed` in `tests/unit/test_notifier_and_cron.py`.
2. Edit `tests/unit/test_notifier_and_cron.py` to fix the test. Use monkeypatch to mock the `src.scheduler.paths` function to return SchedulerPaths located within `tmp_path`, ensuring that the status check does not read the actual user's macOS launchd configuration.
3. Run `.venv/bin/pytest` to verify 100% unit tests pass.
4. Run the backtesting script using `.venv/bin/python scripts/backtest_regime_switch.py` (or similar commands) and extract the final metrics (Sharpe ratio, max drawdown, total trades). Check if they satisfy:
   - Sharpe > 1.0
   - Max Drawdown < 35%
   - Trades >= 100
5. Write your findings, exact command outputs, and the final backtest statistics to `handoff.md` in your working directory.
6. When done, send a message to the orchestrator (conversation ID: ff161eed-9774-4a96-a10c-dd3eea2cb721).

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
