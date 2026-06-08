# BRIEFING — 2026-06-08T09:03:31Z

## Mission
Implement the live execution engine daemon in `src/strategy/regime_live.py` and unit tests in `tests/unit/test_regime_live.py` using FakeExchange.

## 🔒 My Identity
- Archetype: implementer, qa, specialist
- Roles: implementer, qa, specialist
- Working directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/worker_regime_live
- Original parent: 0b7c5f4d-c03f-435b-bf85-9f55cbb4d641
- Milestone: Implement RegimeLiveBot

## 🔒 Key Constraints
- Follow minimal change principle.
- High test coverage for new code.
- No hardcoding test results or creating dummy/facade implementations.
- Write findings, implemented design, and test run output to handoff.md.

## Current Parent
- Conversation ID: 0b7c5f4d-c03f-435b-bf85-9f55cbb4d641
- Updated: not yet

## Task Summary
- **What to build**: `RegimeLiveBot` in `src/strategy/regime_live.py` with indicators, meta state machine execution, ATR sizing logic, market order placement, run/tick loop, DB updates, risk guard checks (cool-off, daily stops, consecutive losses), and graceful halts.
- **Success criteria**: Functional bot that correctly passes tests, maintains real state, and handles live trading flow properly. Unit tests covering all logic.
- **Interface contracts**: `docs/REGIME_SWITCH_STRATEGY.md`, `src/strategy/regime_switch.py`, `src/risk/perp_guards.py`, `src/simple_bot.py`.
- **Code layout**: Source in `src/`, unit tests in `tests/`.

## Key Decisions Made
- [TBD]

## Artifact Index
- `/Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/worker_regime_live/handoff.md` — Final handoff report containing findings, implemented design, and test run output.

## Change Tracker
- **Files modified**: src/strategy/regime_live.py, tests/unit/test_regime_live.py, config/regime_switch.yaml
- **Build status**: TBD
- **Pending issues**: None

## Quality Status
- **Build/test result**: TBD
- **Lint status**: TBD
- **Tests added/modified**: tests/unit/test_regime_live.py

## Loaded Skills
- None
