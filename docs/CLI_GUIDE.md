# tradbot — quick guide

The bot is a terminal app. Two ways to use it:

1. **Type commands** — `python -m scripts.tradbot <command>`
2. **Double-click an app** — build `TradBot.app` once, then click it to
   get an interactive menu (see "macOS app" at the bottom).

The strategy: hold BTC when its daily close is above the 200-day
average (with a 1% buffer), otherwise hold cash. It checks once a day.

---

## One-time setup

```bash
cd ~/TRAD_BOT
python3 -m venv .venv
source .venv/bin/activate          # do this each new terminal
pip install -e ".[dev]"
```

Paper mode (fake money) needs nothing else. Live mode needs a `.env`
with your Binance keys — see `docs/OPERATIONS.md`.

---

## Everyday commands

Run `source .venv/bin/activate` first, then:

| Command | What it does |
|---|---|
| `python -m scripts.tradbot status` | Current state: position, price, equity, holdings. |
| `python -m scripts.tradbot signal` | What the strategy says right now — read-only, no trade. |
| `python -m scripts.tradbot evaluate` | Fetch today's data, decide, and trade if the signal changed. |
| `python -m scripts.tradbot start` | Enable trading. |
| `python -m scripts.tradbot stop` | Disable trading (keeps any open position). |
| `python -m scripts.tradbot flatten --yes` | Sell everything to cash now. |
| `python -m scripts.tradbot trades` | Recent orders with per-trade PnL. |
| `python -m scripts.tradbot equity` | Equity over time + total return. |
| `python -m scripts.tradbot config` | Show the resolved settings and where they came from. |
| `python -m scripts.tradbot watch` | Live-refreshing status (Ctrl+C to stop). |
| `python -m scripts.tradbot menu` | Interactive numbered menu — no need to remember commands. |

Tip — install the package once (`pip install -e .`) and the prefix
shortens to just `tradbot status`, `tradbot menu`, etc.

`tradbot menu` is now a **bot picker** — it opens a top-level menu where
you choose which bot to drive:

1. **Binance trend bot** (the BTC SMA strategy above)
2. **IBKR sentiment bot** (see `docs/IBKR_SENTIMENT.md`)
3. **Funding-arb daemon monitor** (read-only — see below)

`tradbot status` now also shows **peak equity** and **drawdown from
peak**, so you can see at a glance how far below your high-water mark
the account is sitting.

### Monitoring the funding-arb daemon

The funding-arb daemon (`src/main.py`, run on a VPS) persists its state
to `data/bot.db`. These read-only commands surface that state — most
importantly the daily and cumulative loss-stops, with a gauge showing
how much of each loss budget has been consumed:

| Command | What it does |
|---|---|
| `tradbot farb-status` | System status (ACTIVE/HALTED), equity, daily & cumulative PnL, and a headroom gauge for each loss-stop. |
| `tradbot farb-positions` | Open delta-neutral pairs. |
| `tradbot farb-equity` | Recent equity snapshots with daily/cumulative PnL. |

Point it at a different database or config with the `BOT_DB_PATH` and
`BOT_CONFIG` environment variables (handy for inspecting a copy of the
VPS database on your laptop).

---

## Typical day

You don't have to do anything daily if you set up the scheduler (below).
Manually, the routine is:

```bash
tradbot status      # see where things stand
tradbot evaluate    # let the bot act on today's close
```

If the signal hasn't changed, `evaluate` does nothing — that's normal.
The strategy trades only ~6 times a year.

---

## Automate the daily check (macOS)

```bash
python -m scripts.tradbot install-cron
```

Installs a launchd agent that runs `evaluate` every day at 00:05 UTC.
Then you never type `evaluate` again — just glance at `status` when you
feel like it, or wait for the macOS banner when it actually trades.

- `tradbot cron-status` — is the scheduler installed?
- `tradbot logs` — what did the overnight runs do?
- `tradbot uninstall-cron` — remove it.

---

## Backtest / validate before changing anything

```bash
python -m scripts.tradbot backtest          # current config, 5 years
python -m scripts.backtest_trend --years 5  # detailed table
python -m scripts.validate_trend            # walk-forward + out-of-sample
```

### Intraday BB-squeeze strategy (experimental)

A separate, higher-frequency strategy: Bollinger squeeze + RSI<25 entry,
MACD-histogram / mid-band exit on intraday bars. NOT wired into the
live bot yet — validate the numbers first.

```bash
# 6 months of 5-minute BTC bars:
python -m scripts.backtest_bb_squeeze --months 6 --timeframe 5m

# Tighten the volatility filter (require BBW above its 50th percentile):
python -m scripts.backtest_bb_squeeze --months 6 --min-bbw-pct 50

# Try 15m bars or 1h to compare:
python -m scripts.backtest_bb_squeeze --months 6 --timeframe 15m
```

What to look for in the output: win rate ≥55%, net APR > buy-and-hold,
max drawdown comparable to the SMA-200 strategy, and **enough trades
that the result isn't a fluke** (at least ~30 over the window).

---

## Paper vs live

- **Paper** (default): real Binance prices, fake balance. Safe. This is
  what runs unless `SIMPLE_BOT_LIVE=true` is set in `.env`.
- **Live**: real orders. The `status` / `menu` screens show a red
  banner so you always know which mode you're in.

`tradbot reset --yes` wipes the paper database to start fresh. It
refuses in live mode (your real trade history stays).

---

## The macOS app

Build a double-clickable launcher once:

```bash
source .venv/bin/activate
python -m scripts.tradbot install-app
```

This creates `~/Applications/TradBot.app`. Double-click it (or search
"TradBot" in Spotlight) — it opens Terminal and shows the interactive
menu:

```
╭─ TradBot menu · PAPER ─────────────────────╮
│  1  Status                                 │
│  2  Evaluate now (fetch + decide + trade)   │
│  3  Current signal (read-only)              │
│  4  Start trading                           │
│  5  Stop trading                            │
│  6  Recent trades                           │
│  7  Equity history                          │
│  8  Flatten to cash                         │
│  9  Config                                  │
│  0  Quit                                    │
╰─────────────────────────────────────────────╯
Choose (0-9):
```

The app is a thin launcher — it always runs the current code, so you
never have to rebuild it after a `git pull`. The first time it tries
to show a trade notification, macOS will ask permission for
notifications; click Allow.
