## 2026-06-08T09:03:31Z
You are the teamwork_preview_worker. Your task is to implement the live execution engine daemon in `src/strategy/regime_live.py` and implement unit tests in `tests/unit/test_regime_live.py` using `FakeExchange`.

### MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

### Context
Please read docs/REGIME_SWITCH_STRATEGY.md, src/strategy/regime_switch.py, src/risk/perp_guards.py, and src/simple_bot.py.

### Requirements
1. Implement `src/strategy/regime_live.py`. It should define a `RegimeLiveBot` class (or similar name) that:
   - Wires up the exchange adapter (supporting DRY_RUN, PAPER, LIVE modes), database (`data/bot.db`), notifier, and config parameters from a YAML file (e.g. `config/regime_switch.yaml`).
   - Wires the indicators and the meta state machine (`evaluate_live` in `src/strategy/regime_switch.py`).
   - Sizing logic: Sizes position off ATR:
     - `stop_distance = atr_mult * ATR`
     - `risk_budget = equity * risk_per_trade_pct` (default: 1% of equity)
     - `qty = risk_budget / stop_distance`, capped at `max_leverage = 3`.
     - Round to `qty_step` and check `min_qty` (fetch from exchange/symbol config).
   - Order submission:
     - Opening: Set leverage first. Submit market order via `submit_order` to the exchange. If filled, create/open a `Position` in DB with `spot_qty=0`, `perp_qty=qty` (signed), and record order/fill.
     - Closing: Submit market order of opposite side with `reduce_only=True`. If filled, close `Position` in DB, compute realized PnL.
   - Run/Tick loop:
     - Runs continuously, waking up near the close of each bar (5m/15m/1h based on config).
     - At bar close, fetches recent OHLCV history, checks active positions (e.g., checks if stop price is hit using current price or bar low/high, or if strategy signals exit), checks risk guards (cool-off, daily stops, consecutive losses, etc.), and executes trade signals.
     - Logs status and updates a `StateSnapshot` in DB.
     - Gracefully halts trading (setting status to HALTED in DB) if any breaker triggers (e.g. daily/cumulative stops, consecutive losses).
2. Implement unit tests in `tests/unit/test_regime_live.py`.
   - Mock exchange adapter or use `FakeExchange` to simulate ticks, bar closes, signals, order execution, PnL updates, and risk halts.
   - Target high test coverage (ideally 100%) for the new code.
3. Run the unit tests and verify correctness.

Write your findings, implemented design, and test run output to `/Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/worker_regime_live/handoff.md`.

## 2026-06-08T13:59:19Z
The server has restarted and all subagents have been stopped. Please read your BRIEFING.md, check your current status, recover your context from progress.md, and resume implementation of the Live Execution Daemon (regime_live.py) & FakeExchange Tests (Milestone 2).

