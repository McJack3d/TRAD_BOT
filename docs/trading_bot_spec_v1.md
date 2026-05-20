# Funding Rate Arbitrage Bot — Specification v1

**Project codename:** TBD
**Author:** Alexandre Bredillot
**Date:** May 2026
**Status:** v1 — pre-implementation
**Capital at risk (full deployment):** €1,000
**Capital at risk (micro-live):** €100–150
**Target build timeline:** ~3 months at 10–20 hrs/week

---

## 1. Executive summary

The bot executes a **delta-neutral cash-and-carry basis trade** on Binance: long spot, short perpetual futures, in equal notional. The strategy harvests the funding rate paid by perpetual longs to perpetual shorts every 8 hours. The bot is fully autonomous, runs on a Singapore VPS, and is monitored via Telegram (real-time) and email (daily summaries).

The starting capital of €1,000 is treated as tuition. The success metric for v1 is not absolute returns — it is **a robust, observable, recoverable system that survives one full quarter of live market regimes without manual intervention or capital loss exceeding stop thresholds.**

---

## 2. Strategy overview

### 2.1 The edge

Crypto perpetual futures have no expiry. To tether the perp price to the underlying spot, exchanges apply a **funding rate** every 8 hours. When perps trade above spot (typical when retail longs lever up), longs pay shorts. When perps trade below spot, shorts pay longs. Positive funding has been the dominant regime on BTC/ETH perps since 2020.

### 2.2 The trade

For each tracked symbol (BTC, ETH, SOL):

1. Buy `N` USD of spot.
2. Open a short perp position of `N` USD notional, isolated margin, leverage capped at 2x.
3. Hold through funding settlements (every 8h: 00:00, 08:00, 16:00 UTC).
4. Exit (close both legs simultaneously) when one of these triggers fires:
   - Funding rate falls below exit threshold (parameter, default 0.005% per 8h)
   - Short-leg liquidation distance falls below 20% of margin
   - Daily or cumulative stop hit
   - Manual kill via Telegram
   - Hard system failure (see §11)

### 2.3 Why this edge for this builder

- Real, persistent inefficiency tied to retail leverage behavior — not a vibe.
- Market-neutral by construction; P&L doesn't depend on price direction.
- Works at €1k size where most strategies don't.
- Forces deep learning of exchange APIs, margin mechanics, and reconciliation — transferable infrastructure for any future bot.
- Modest but consistent expected returns (5–10% APR realistic post-cost in 2026 regime); fits the "tuition not get-rich-quick" framing.

### 2.4 Known risks

| Risk | Mitigation |
|---|---|
| Counterparty (exchange insolvency) | Single-exchange limit on capital; future v2 spreads across exchanges |
| Short-leg liquidation on price spike | Isolated margin, 2x cap, auto top-up logic, liquidation-distance halt |
| Funding regime flip (longs collecting) | Exit on funding below threshold; bot doesn't fight the regime |
| API outage during settlement | Resilient retry; reconciliation loop; halt on hard failure |
| Edge compression | Expected; threshold tuning via periodic re-backtest |
| Tax complexity | Per-trade log with all fields needed for French PFU + form 3916-bis |

---

## 3. System architecture

### 3.1 Components

```
┌─────────────────────────────────────────────────────────────┐
│                    Singapore VPS                            │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐ │
│  │ Market Data  │───▶│  Strategy    │───▶│  Execution   │ │
│  │   (WS+REST)  │    │   Engine     │    │   Engine     │ │
│  └──────────────┘    └──────┬───────┘    └──────┬───────┘ │
│                              │                    │         │
│                       ┌──────▼────────────────────▼──────┐ │
│                       │       Risk Manager               │ │
│                       │  (pre-trade + continuous)        │ │
│                       └──────┬───────────────────────────┘ │
│                              │                              │
│  ┌──────────────┐    ┌──────▼───────┐    ┌──────────────┐ │
│  │ Reconciler   │◀──▶│   State /    │───▶│  Monitoring  │ │
│  │  (loop)      │    │   SQLite     │    │  (TG+Email)  │ │
│  └──────────────┘    └──────────────┘    └──────────────┘ │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                      ┌───────────────┐
                      │  Binance API  │
                      │  (spot+perp)  │
                      └───────────────┘
```

### 3.2 Module responsibilities

- **Market Data** — Maintains live WebSocket connections (spot + perp orderbook tops, funding rate, mark price). REST fallback on WS drop. Exposes a clean async interface to the strategy.
- **Strategy Engine** — Evaluates entry/exit conditions per symbol on each funding-rate update. Stateless per-tick; reads from State.
- **Risk Manager** — Pre-trade checks (size, exposure, liquidation distance) and continuous monitoring (daily P&L, cumulative drawdown). **Has unilateral authority to flatten and halt.**
- **Execution Engine** — Submits orders to Binance via `ccxt` or native client. Handles partial fills, retries, idempotency, two-leg coordination.
- **State** — SQLite database. Single source of truth for positions, orders, fills, funding payments, parameters.
- **Reconciler** — Every 60 seconds, compares internal state to Binance-reported positions and balances. On mismatch beyond tolerance, raises alert and halts new orders.
- **Monitoring** — Telegram bot for real-time alerts and interactive commands (kill switch, status, pause); email for daily/weekly summaries.

### 3.3 Deployment

- **Host:** Hetzner CAX11 (ARM, 2 vCPU, 4GB RAM, Singapore) or DigitalOcean Singapore droplet, ~€5/month.
- **OS:** Ubuntu 24.04 LTS.
- **Process supervision:** `systemd` unit with auto-restart, journald logging.
- **Time sync:** `chrony`; clock drift > 100ms triggers halt.
- **Network:** Outbound HTTPS to Binance only; SSH on non-standard port with key-only auth; UFW firewall denying all inbound except SSH.
- **VPN consideration:** Binance KYC and web-UI access from Vietnam IP is geofenced. Use VPN to French exit for any web/account work. API access from Singapore VPS is fine and IP-whitelisted (see §10).

---

## 4. Trading parameters

| Parameter | Default value | Notes |
|---|---|---|
| Tracked symbols | BTCUSDT, ETHUSDT, SOLUSDT | Spot + perpetual pairs |
| Position sizing | Equal-weight across active symbols | Max 50% of capital in single symbol |
| Max gross notional per symbol | 50% of equity | Hard limit |
| Perpetual leverage | 2x max | Isolated margin |
| Entry: funding threshold | 0.02% per 8h (≈22% APR equiv.) | Tuned via walk-forward backtest |
| Exit: funding threshold | 0.005% per 8h (≈5.5% APR equiv.) | Hysteresis to avoid flip-flopping |
| Min position dwell time | 24 hours | Avoid churning across single funding events |
| Liquidation distance halt | 20% of initial margin | Flatten short leg if breached |
| Margin top-up trigger | 30% of initial margin | Add USDT to short-leg margin |
| Daily loss stop | 2% of capital (€20 on €1k) | Realized + unrealized over rolling 24h |
| Cumulative loss stop | 10% of capital (€100 on €1k) | From starting equity |
| Order rate limit | Max 10 orders / 60s | Catches runaway loops |
| Funding cycle | Every 8h: 00:00, 08:00, 16:00 UTC | Strategy evaluation tick |

All parameters live in a single Pydantic-validated config file (`config/live.yaml`, `config/paper.yaml`, `config/backtest.yaml`).

---

## 5. Risk management

### 5.1 Pre-trade checks (every order)

Order is rejected if any check fails:

1. Total exposure post-order ≤ max gross notional.
2. Per-symbol exposure ≤ symbol cap.
3. Short-leg liquidation distance post-order ≥ 30% of margin.
4. Order rate within rate-limit window.
5. Daily P&L not breached.
6. Cumulative P&L not breached.
7. Reconciliation status = OK (no drift detected).
8. System status = ACTIVE (not halted).

### 5.2 Continuous monitoring (every 10 seconds)

The Risk Manager loop independently re-evaluates:

- Rolling 24h realized + unrealized P&L vs. daily stop.
- Cumulative P&L vs. cumulative stop.
- Liquidation distance on each open short leg.
- Margin headroom; auto-top-up if below trigger.

Any breach → flatten all positions, set system status to HALTED, emit Telegram alert with full context.

### 5.3 Position sizing logic

For each active symbol:
- `target_notional_per_symbol = (equity * 0.5) / n_active_symbols`
- Capped at the symbol-specific exposure ceiling.
- Re-evaluated on every entry; existing positions are not resized mid-trade.

### 5.4 The kill switch

Three independent kill paths:

1. **Telegram button:** `/kill` command or inline button → flatten + halt + require manual unhalt.
2. **VPS-side file:** presence of `/var/lib/bot/KILL` file → bot detects within 5 seconds, flattens, halts.
3. **Exchange-side:** revoke API key on Binance dashboard → bot fails next call, enters halt-on-hard-failure path.

---

## 6. Development phases

### Phase 0 — Foundation (Week 1–2)
- Project scaffolding, dependency setup, config schema
- `ccxt` integration tested against Binance testnet
- SQLite schema for state, trades, funding payments
- Logging + structured JSON logs to journald
- Skeleton modules with stub interfaces

### Phase 1 — Market data + state (Week 2–4)
- WebSocket clients for spot and perp data
- Funding rate poller (REST, every minute as fallback)
- Reconciliation loop comparing internal state to Binance-reported balances
- Unit tests on reconciliation edge cases

### Phase 2 — Strategy + risk (Week 4–6)
- Strategy engine with entry/exit logic
- Risk Manager module with all checks from §5
- **Risk Manager has a dedicated test suite with adversarial inputs** (NaN P&L, negative margin, duplicate fills, etc.)
- Backtester (event-driven, realistic fee/slippage model)

### Phase 3 — Execution + monitoring (Week 6–8)
- Order submission, partial-fill handling, two-leg coordination
- Telegram bot (commands + inline buttons)
- Email digest sender (daily, weekly)
- Local dashboard (Grafana or simple Streamlit) for live PnL, positions, funding history

### Phase 4 — Backtest + tuning (Week 8–10)
- Pull full Binance funding-rate history (2020-onward)
- Walk-forward backtest with rolling 6-month train / 1-month test windows
- Sensitivity analysis on entry/exit thresholds
- Document expected performance metrics (Sharpe, max DD, hit rate, avg dwell time)

### Phase 5 — Dry-run + go-live (Week 10–12)
- 5–7 days dry-run on production VPS (full logic, order submission intercepted and logged)
- Triage any bugs surfaced
- Switch to micro-live with €100–150
- 2 weeks observation
- Scale to full €1k if Phase 5 acceptance criteria pass (see §15)

---

## 7. Go-live path

| Phase | Capital | Duration | Purpose | Exit gate |
|---|---|---|---|---|
| Dry-run | €0 | 5–7 days | Catch catastrophic bugs without financial risk; intercept all order submissions and log instead of send | Zero unhandled exceptions; reconciliation clean for full period; at least one survived WS drop |
| Micro-live | €100–150 | 14 days | Surface real-execution frictions (slippage, exchange rejections, psychology) | No catastrophic discrepancies vs. expected; no manual interventions needed; risk module respected all limits |
| Full live | €1,000 | Ongoing | Production | Phase-5 acceptance criteria met (§15) |

If any phase fails its gate, return to the previous phase with fixes applied.

---

## 8. Backtesting methodology

### 8.1 Data
- **Source:** Binance REST historical endpoints + funding-rate archive.
- **Period:** 2020-01-01 through current date, all available.
- **Symbols:** BTCUSDT, ETHUSDT, SOLUSDT.
- **Resolution:** 1-minute spot OHLCV, perp mark price, funding rate per cycle.
- **Storage:** Parquet files, partitioned by symbol and month.

### 8.2 Methodology
- **Walk-forward:** rolling 6-month train / 1-month test windows.
- **In-sample / out-of-sample split:** 70 / 30, with the final 30% never touched until v1 validation.
- **Fee model:** Binance taker = 0.04% per side (assume taker for conservatism). Maker rebate not modeled in v1.
- **Slippage model:** assume 0.02% adverse on each leg for orders within typical size; revisit when live data accumulates.
- **Funding accounting:** mark-to-market every 8h boundary using actual historical funding rate.

### 8.3 Metrics to report
- Net APR (post-cost)
- Sharpe ratio (using daily returns)
- Max drawdown
- Time in market (% of period with active positions)
- Average dwell time per position
- Distribution of per-trade P&L
- Performance per symbol
- Performance per regime (bull, bear, chop — manually labeled or via 90-day MA slope)
- Stress test: FTX week (Nov 2022) — does the bot survive?

### 8.4 Anti-overfitting discipline
- Threshold parameters tuned only on training window; reported on test window.
- Maximum 3 hyperparameters tuned (entry threshold, exit threshold, min dwell).
- Reject any threshold combination requiring more than 2 decimal places of precision.
- Bayesian skepticism: if backtest Sharpe > 3, assume overfit and re-examine.

---

## 9. Monitoring & operations

### 9.1 Telegram (real-time)

Inline buttons for: `🔴 KILL ALL`, `⏸ PAUSE`, `▶ RESUME`, `📊 STATUS`.

Commands:
- `/status` — current positions, P&L, system state
- `/positions` — open positions with liquidation distance
- `/funding` — current funding rates across tracked symbols
- `/halt` — soft halt (no new orders, existing positions stay)
- `/kill` — flatten everything and halt
- `/resume` — exit halted state (requires confirmation)

Push notifications on:
- Any entry or exit
- Any risk-manager trigger (loss stop, liquidation distance, margin top-up)
- Any reconciliation mismatch
- Any unhandled exception
- WebSocket drops longer than 30s
- System halt or resume

### 9.2 Email (digests)

- **Daily (08:00 UTC):** P&L, positions held, funding collected, any events from prior 24h.
- **Weekly (Monday 08:00 UTC):** week's P&L, comparison to backtest expectation, drawdown, position turnover.
- **Monthly:** full performance report + tax-export CSV (see §12).

### 9.3 Dashboard

Local Streamlit app or Grafana board exposing:
- Live equity curve
- Open positions and their per-position P&L
- Funding rate time series for tracked symbols (current + last 30 days)
- Reconciliation status and last successful check
- System health (WS connection state, latency to exchange, last error)

Available only over SSH tunnel from the VPS; not exposed publicly.

---

## 10. Security

### 10.1 API keys

- **No-withdrawal permission.** API keys are scoped to trading and reading only; withdrawal capability never enabled.
- **IP whitelist** to the VPS public IP on Binance API key settings. Any request from another IP is rejected by Binance itself.
- **Separate keys per environment** (testnet, dry-run, live) — keys never reused or shared between environments.
- **Keys stored** in `pass` or `gopass` on the VPS, decrypted only into process memory at startup; never written to disk in plaintext, never committed to git.

### 10.2 VPS hardening

- SSH on non-standard port, key-only auth, root login disabled.
- UFW firewall: deny all inbound except SSH; allow all outbound (broker connectivity needs it).
- `fail2ban` for SSH brute-force protection.
- Automatic security updates via `unattended-upgrades`.
- Bot runs as non-root user.

### 10.3 Code & secrets

- Git repository: private GitHub.
- Secrets in environment variables loaded from `.env` (gitignored). `.env.example` committed with placeholder names only.
- Pre-commit hook (e.g., `detect-secrets`) to block accidental key commits.
- No third-party telemetry, analytics, or crash-reporting that could leak account state.

---

## 11. Failure handling

**Posture:** resilient with exponential backoff; halt on hard failure.

### 11.1 Transient failures (retry)

| Failure | Action |
|---|---|
| HTTP 429 (rate limit) | Wait per `Retry-After`, retry up to 5x |
| HTTP 5xx | Exponential backoff 1s, 2s, 4s, 8s, 16s; halt after |
| WebSocket disconnect | Reconnect with backoff; REST polling fills gap |
| Timeout on order submission | Query order status by client-order-id (idempotency); never blindly resubmit |
| Reconciliation drift within tolerance | Log and continue; tighten on persistent drift |

### 11.2 Hard failures (halt)

System enters HALTED state, sends Telegram alert with full context, requires manual investigation and explicit `/resume`:

- Reconciliation drift beyond tolerance (position size mismatch > 0.1% or balance mismatch > €1)
- API key rejected (HTTP 401 / 403)
- Clock drift > 100ms
- Daily or cumulative loss stop hit
- Liquidation distance breached
- Unhandled exception in strategy or risk module
- Database write failure
- Repeated (>3) consecutive failures of the same critical operation

### 11.3 The reconciliation loop

Every 60 seconds, the Reconciler:
1. Fetches Binance-reported balances and open positions.
2. Compares to internal state.
3. On match: updates `last_reconciliation_ok` timestamp.
4. On mismatch within tolerance: logs warning.
5. On mismatch beyond tolerance: halts new orders, alerts.

If `last_reconciliation_ok` is older than 5 minutes, the bot halts.

---

## 12. Tax & compliance (France)

### 12.1 Tax regime
- French residents pay **PFU (flat tax) of 30%** on realized crypto gains (12.8% income tax + 17.2% social contributions).
- Foreign account declaration: **form 3916-bis** required annually for any non-French crypto exchange account, regardless of activity level. Late filing = €1,500 minimum penalty.

### 12.2 What the bot must export

Monthly CSV with every transaction containing:
- Timestamp (UTC + Europe/Paris)
- Symbol
- Side (buy/sell, long/short)
- Quantity (base asset)
- Price (USDT)
- Fee paid (USDT)
- Trade ID (Binance)
- P&L attribution (entry/exit/funding)
- Cumulative position after trade

Funding payments treated as taxable income at receipt, valued in EUR at the funding-event timestamp (using daily ECB rate).

### 12.3 Caveat

Tax treatment of perp short-leg P&L and funding income in France is not fully settled. I am not a tax advisor. Plan to consult a French crypto tax accountant (e.g., Waltio, Coinhouse, or an independent expert-comptable familiar with crypto) before filing the first declaration that includes bot activity.

---

## 13. Project structure (suggested)

```
trading-bot/
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   ├── live.yaml
│   ├── paper.yaml
│   └── backtest.yaml
├── src/
│   ├── adapters/
│   │   ├── exchange_base.py       # abstract interface
│   │   └── binance.py             # Binance implementation
│   ├── data/
│   │   ├── market_data.py
│   │   └── historical.py
│   ├── strategy/
│   │   ├── funding_arb.py
│   │   └── signals.py
│   ├── risk/
│   │   ├── manager.py
│   │   └── checks.py
│   ├── execution/
│   │   ├── engine.py
│   │   └── order.py
│   ├── state/
│   │   ├── db.py
│   │   └── models.py
│   ├── reconciliation/
│   │   └── reconciler.py
│   ├── monitoring/
│   │   ├── telegram_bot.py
│   │   ├── email.py
│   │   └── dashboard.py
│   ├── backtest/
│   │   ├── engine.py
│   │   ├── walk_forward.py
│   │   └── metrics.py
│   └── main.py
├── tests/
│   ├── unit/
│   │   ├── test_risk_manager.py   # exhaustive, adversarial
│   │   ├── test_reconciler.py
│   │   └── test_strategy.py
│   └── integration/
│       └── test_binance_testnet.py
├── scripts/
│   ├── download_history.py
│   ├── run_backtest.py
│   ├── run_dry_run.py
│   └── tax_export.py
└── deploy/
    ├── systemd/
    │   └── bot.service
    └── setup.sh
```

---

## 14. Tech stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | Latency budget allows; ecosystem fit |
| Async runtime | `asyncio` + `uvloop` | Standard for I/O-bound work |
| Exchange | `ccxt` (initial) + native Binance client (where needed) | Speed of development + escape hatch for performance |
| HTTP | `httpx` | Async, modern |
| WebSocket | `websockets` | Async, well-maintained |
| Config | `pydantic-settings` | Typed config, env override |
| Database | SQLite + `sqlalchemy` (or raw `sqlite3`) | Single-instance, zero ops |
| Logging | `structlog` → journald (JSON) | Structured, queryable |
| Telegram | `python-telegram-bot` | De facto |
| Email | `aiosmtplib` + Gmail App Password (or SES) | Simple |
| Backtester | Custom event-driven (no `vectorbt`) | Realistic fills + funding accounting |
| Data store | Parquet + `pyarrow` | Columnar, fast, portable |
| Testing | `pytest` + `pytest-asyncio` | Standard |
| CI | GitHub Actions | Free, sufficient |
| Process supervision | `systemd` | Native, reliable |
| Secrets | `pass` (`gopass`) or env vars from a `.env` not committed | No vendor lock-in |
| Dashboard | Streamlit (start) or Grafana (later) | Streamlit ships in a day |

---

## 15. Acceptance criteria for v1 → full live

Bot is considered v1-complete and eligible for full €1k deployment when **all** of the following hold:

1. All unit tests pass; risk-manager tests include at least 30 adversarial cases and all pass.
2. Backtest over 2020–2025 shows: positive net APR after costs, max drawdown < 15%, survives FTX-week stress test.
3. Walk-forward out-of-sample Sharpe > 0.5 with stable threshold parameters across windows.
4. 5–7 day dry-run on VPS completes with: zero unhandled exceptions, reconciliation clean throughout, at least one WebSocket drop survived cleanly.
5. 14-day micro-live completes with: no manual interventions, all risk limits respected, realized P&L not catastrophically out of line with expectations (use judgment, not a hard ±x% gate at this size where fees dominate).
6. Telegram kill switch tested and confirmed functional in production.
7. Monthly tax-export CSV produced and reviewed for correctness.

---

## 16. Out of scope for v1 (deferred to v2+)

- Multi-exchange (Bybit, OKX, Coinbase) — architecture supports it, implementation deferred.
- Cross-exchange arbitrage (funding differential between venues).
- Maker-fee optimization (limit-order ladders to capture rebate).
- Additional strategies (mean-reversion overlay, directional trend filter).
- Mobile-native dashboard (Telegram is enough for v1).
- Automated tax-form generation (CSV export is sufficient).
- Sub-second order routing or co-location.
- Multi-user / multi-account support.

---

## 17. Open questions to resolve before coding starts

1. **Binance account jurisdiction** — does Alex have an existing Binance account opened from France? If not, opening one from VPN-Singapore raises KYC complications. Confirm before Phase 0.
2. **Project codename** — needs one for the GitHub repo and config namespacing.
3. **Email vendor** — Gmail App Password is simplest; AWS SES is cheaper at scale but adds setup. Default to Gmail unless preference.
4. **Backtest data acquisition** — full Binance history via REST is slow (rate-limited). Decide between (a) patient REST download with checkpointing, (b) third-party data provider (e.g., Tardis.dev — paid), (c) accept shorter history.

---

## 18. Glossary

- **Basis trade / cash-and-carry** — long spot + short futures, profiting from the spread converging at expiry (or, for perps, from the funding rate).
- **Funding rate** — periodic payment between perp longs and shorts to tether perp price to spot. Positive funding = longs pay shorts.
- **Isolated margin** — margin allocated to a specific position; loss capped at that allocation (no cross-position contagion).
- **Liquidation distance** — how far the price can move adversely before the position is force-closed by the exchange.
- **Walk-forward** — backtesting methodology where parameters are tuned on a rolling training window and evaluated on the next held-out window.
- **PFU** — Prélèvement Forfaitaire Unique, France's 30% flat tax on capital income including crypto gains.
- **Form 3916-bis** — French tax form for declaring foreign financial accounts, including crypto exchange accounts.
- **Dry-run / shadow mode** — bot runs full logic but order submission is intercepted and logged instead of sent.
- **Reconciliation** — periodic comparison between the bot's internal record of state and the exchange's authoritative record.

---

*End of v1 specification. Next step: resolve §17 open questions, then begin Phase 0 implementation.*
