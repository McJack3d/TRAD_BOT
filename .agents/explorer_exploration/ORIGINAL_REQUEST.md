## 2026-06-08T08:56:10Z

You are the Codebase Explorer. Your task is to investigate the TRAD_BOT codebase and provide a comprehensive report to help implement the Regime-Switching Long/Short Perp Bot (live execution engine) on Binance.

Specifically, explore and report on:
1. The existing perp execution capability in ExchangeAdapter (src/adapters/exchange_base.py), BinanceAdapter (src/adapters/binance.py), PaperBinanceAdapter (src/adapters/paper_binance.py), and FakeExchange (src/adapters/fake.py). Do we already have methods to set leverage, submit perpetual orders (including reduce-only orders), fetch positions, and close perp positions?
2. The current database structure (src/state/db.py, src/state/models.py). How are positions, orders, fills, and system status managed? What tables or models should we reuse or add?
3. How the existing daemons/bots are structured, especially simple_bot.py and the funding-arb bot (if any). How does SimpleBot implement its tick/evaluate loop? How is it run?
4. The current risk check mechanism (src/risk/checks.py, src/risk/manager.py) and how they can be extended or reused for the new `src/risk/perp_guards.py`.
5. How we can run existing unit/integration tests and verify that the setup is clean before we make changes. Run the test suite and report the command and results.

Write your findings to handoff.md in your working directory: /Users/alexandrebredillot/Documents/GitHub/TRAD_BOT/.agents/explorer_exploration/handoff.md.
Ensure all findings are backed by file paths and specific code structures.
