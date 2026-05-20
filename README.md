# trad-bot

Funding-rate arbitrage bot for Binance. Delta-neutral cash-and-carry:
long spot + short perp in equal notional, harvest funding every 8h.

See `docs/trading_bot_spec_v1.md` for the full spec. This README covers
project layout, install, and operations.

## Status

v1 — pre-deployment. The strategy, risk, execution, and reconciliation
modules are implemented. Backtester is event-driven and runs against
local Parquet history. Telegram and email monitoring wired up. Live
trading **only after** the §15 acceptance gates in the spec are met.

## Layout

```
src/
  adapters/        Exchange adapter (abstract base + Binance impl via ccxt)
  data/            Live market data (WS+REST) and historical data loader
  strategy/        Funding-arb signal engine
  risk/            Pre-trade checks + continuous monitoring + kill switch
  execution/       Two-leg order coordination with idempotency
  state/           SQLite schema and DAOs
  reconciliation/  Internal-vs-exchange state diff loop
  monitoring/      Telegram bot, email digests, dashboard
  backtest/        Event-driven backtester + walk-forward + metrics
  config.py        Pydantic-validated settings (env + YAML)
  main.py          Entry point (asyncio)
config/            live.yaml / paper.yaml / backtest.yaml
scripts/           Download history, backtest, dry-run, tax export
tests/             Unit (incl. adversarial risk-manager) + integration
deploy/            systemd unit + setup script
```

## Install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in
```

## Run modes

```bash
# Backtest
python -m scripts.run_backtest --config config/backtest.yaml

# Dry-run (full logic, order submission intercepted and logged)
python -m scripts.run_dry_run --config config/paper.yaml

# Live (only after acceptance gates pass)
BOT_ENV=live python -m src.main --config config/live.yaml
```

## Tests

```bash
pytest                          # unit tests
pytest -m integration           # integration (needs testnet keys)
```

## Safety

- No-withdrawal API keys. IP-whitelist on Binance side.
- Risk Manager has unilateral authority to flatten and halt.
- Three kill paths: Telegram `/kill`, VPS file `/var/lib/bot/KILL`,
  revoke the API key on Binance.
- Reconciliation every 60s; halt if stale > 5min.

## Deploy

See `deploy/setup.sh` and `deploy/systemd/bot.service`. Target host is
Hetzner CAX11 (ARM, Singapore) or DO Singapore. Ubuntu 24.04.
