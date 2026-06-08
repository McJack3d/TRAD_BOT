## 2026-06-08T14:10:15Z

You are the Forensic Auditor. Your task is to perform a comprehensive integrity audit on the implementation of the Regime-Switching Long/Short Perp Bot inside this repository.

Specifically:
1. Verify that the implemented code does not contain hardcoded test results, expected outputs, or dummy/facade implementations.
2. Check that the risk controls (perp_guards.py) and execution logic (regime_live.py) are genuinely implemented with the correct logic from REGIME_SWITCH_STRATEGY.md.
3. Check the CLI commands in scripts/tradbot_regime.py for genuine behavior.
4. Run the full unit and integration test suite to verify correctness and report the results and commands used.
   - Run tests: `pytest tests/unit/test_perp_guards.py`
   - Run tests: `pytest tests/unit/test_regime_live.py`
   - Run tests: `pytest tests/unit/test_tradbot_regime.py`
   - Run all tests to make sure there are no regressions: `pytest -m "not integration"` (or similar syntax if appropriate).

Write your audit report and final verdict (CLEAN or VIOLATION DETECTED) to handoff.md in your working directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/auditor/handoff.md.
