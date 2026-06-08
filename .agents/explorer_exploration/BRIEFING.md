# BRIEFING — 2026-06-08T08:58:30Z

## Mission
Investigate TRAD_BOT codebase and produce a report to help implement a Regime-Switching Long/Short Perp Bot on Binance.

## 🔒 My Identity
- Archetype: explorer
- Roles: Codebase Explorer
- Working directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/explorer_exploration
- Original parent: 0b7c5f4d-c03f-435b-bf85-9f55cbb4d641
- Milestone: codebase-exploration

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- CODE_ONLY network mode

## Current Parent
- Conversation ID: 0b7c5f4d-c03f-435b-bf85-9f55cbb4d641
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `src/adapters/exchange_base.py`
  - `src/adapters/binance.py`
  - `src/adapters/paper_binance.py`
  - `src/adapters/fake.py`
  - `src/state/models.py`
  - `src/state/db.py`
  - `src/simple_bot.py`
  - `scripts/tradbot.py`
  - `src/main.py`
  - `src/risk/checks.py`
  - `src/risk/manager.py`
- **Key findings**:
  - Exchange adapters already support leverage setting, order submission (incl. reduce_only), and position fetching. No explicit `close_position` method exists; must submit opposing order with `reduce_only=True`.
  - Database is SQLite with SQLAlchemy. `Position` model is spot long + perp short delta-neutral pair, but can support directional perp if `spot_qty=0`.
  - SimpleBot is one-shot evaluate/tick script, whereas live daemon is persistent.
  - RiskManager continuous loop only monitors short perps; long perps are ignored and need guards.
- **Unexplored areas**:
  - None (completed all tasks).

## Key Decisions Made
- Initial scan using search/view tools to build understanding of the five key topics.
- Documented findings in handoff.md.

## Artifact Index
- /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/explorer_exploration/handoff.md — Final structured report of the codebase exploration.
