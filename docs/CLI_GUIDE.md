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
