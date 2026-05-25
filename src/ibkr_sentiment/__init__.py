"""IBKR sentiment bot.

A second, fully independent bot that lives alongside the Binance
funding-arb daemon. Trades US equities through Interactive Brokers
(IB Gateway + ib_insync) driven by a multi-stage sentiment pipeline:

    Stage 1: FinBERT discriminative filter (high-throughput screen)
    Stage 2: Generative LLM gatekeeper (contextual reasoning, CoT)

Outputs a numeric sentiment factor that is combined with technical
confirmation (SMA / RSI) and routed through a dollar-neutral
long/short overlay before any order is placed.

Modules
-------
sentiment/        the two-stage funnel + ingestion + vector store
broker/           ib_insync wrapper, Redis rate limiter, paper broker
signal_engine/    sentiment-to-signal mapping, technical confirms,
                  dollar-neutral basket construction
execution/        order routing + position management
risk/             pre-trade and continuous risk overlay
state/            async SQLAlchemy DAOs (Postgres/TimescaleDB or SQLite)
config            Pydantic settings tree (see config/ibkr_sentiment.yaml)
bot               top-level orchestrator
main              CLI entry point
"""
