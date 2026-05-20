# Paper trading and performance validation

Three complementary ways to evaluate the bot before risking real money:

1. **Historical backtest** — fastest. Run the strategy on 5 years of
   real BTC data in ~10 seconds. Tells you what the strategy *would
   have* done.
2. **Local paper trading** — run the bot live against real Binance
   prices with fake balances. Tells you what it *is* doing right now.
3. **Public paper-trading link** — same as (2) but reachable from your
   phone or shareable with someone else.

---

## 1. Historical backtest (recommended first)

```bash
source .venv/bin/activate

# Default: 5 years, SMA-200, 1% buffer, 15% trailing stop, $1000.
python -m scripts.backtest_trend

# Override any parameter:
python -m scripts.backtest_trend --years 7 --sma 100 --trailing-stop 0.20

# Skip the trailing stop:
python -m scripts.backtest_trend --trailing-stop 0
```

### Full validation suite (recommended before going live)

```bash
python -m scripts.validate_trend
```

Runs four checks back-to-back:
1. In-sample full-window backtest with rich metrics (Sharpe / Sortino /
   Calmar / Ulcer Index, beyond just APR + drawdown).
2. Out-of-sample split: tunes nothing on the last 30% of data and
   reports the honest forward-looking number on that held-out window.
3. Walk-forward: rolling 2-year train / 6-month test windows. Tunes
   `(sma_window, buffer)` on each train window via grid search, reports
   the test-window result. Strategy is robust iff most test Sharpes > 0.
4. Per-asset generalization: same params on BTC, ETH, SOL individually.
   The rule shouldn't only work on the asset you tuned on.

You get a table comparing the strategy to buy-and-hold:

```
SMA-50 trend follower vs. buy-and-hold
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ metric        ┃      strategy ┃   buy & hold ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ Initial       │      $1,000   │      $1,000  │
│ Final         │      $X,XXX   │      $Y,YYY  │
│ Net APR       │       NN.N%   │       MM.M%  │
│ Max drawdown  │      -NN.N%   │      -MM.M%  │
│ Trades        │            N  │           1  │
└───────────────┴───────────────┴──────────────┘
```

Interpretation:
- **Strategy APR > B&H APR**: the rule adds value through the cycle.
- **Strategy APR ≈ B&H APR but lower max DD**: same return, smoother
  ride — that's the trade-off the strategy actually offers.
- **Strategy APR < B&H APR with similar max DD**: the rule cost you
  money. Don't go live with these parameters.

---

## 2. Local paper trading

```bash
source .venv/bin/activate
streamlit run src/app/streamlit_app.py
# → http://localhost:8501
```

The page uses **real-time Binance prices** (public REST, no key needed)
and a **simulated $1,000 portfolio** that tracks BTC and USDT
balances. Click **Start trading**, then **Evaluate now** to take the
first signal. Each evaluation:

- Fetches the current BTC price.
- Computes the SMA-50 signal from the last 55 daily closes.
- Flips position if the signal disagrees with current holdings.
- Snapshots the equity to the local DB.

The equity curve plot in the right pane updates with every evaluation.
Leave the terminal running for a week, click *Evaluate now* once a day,
and you'll have a real paper-trading record on real market data.

To reset the paper portfolio:

```bash
rm data/simple_bot.db
```

To start with a different paper balance:

```bash
SIMPLE_BOT_STARTING_USDT=5000 streamlit run src/app/streamlit_app.py
```

---

## 3. Public link (paper trading from your phone)

You have two free options.

### Option A — `ngrok` tunnel (simplest, ephemeral URL)

Install `ngrok` (one-time), then:

```bash
# Terminal 1
streamlit run src/app/streamlit_app.py

# Terminal 2
ngrok http 8501
```

ngrok prints a URL like `https://xxxx-yyyy.ngrok-free.app`. Open that
on your phone — the Streamlit UI works the same. Closing the ngrok
terminal kills the public URL; closing the Streamlit terminal kills the
bot.

**Caveat**: anyone with the URL can press buttons. In paper mode the
worst they can do is reset the fake balance. Don't use ngrok with
`SIMPLE_BOT_LIVE=true` unless you also add a password (ngrok supports
this on the paid plan).

### Option B — Streamlit Community Cloud (permanent URL)

Free, auto-deploys from GitHub.

1. Push your branch to a GitHub repo you control (the current branch
   `claude/create-bot-83gZr` is fine).
2. Go to https://streamlit.io/cloud → "New app".
3. Select the repo, branch `claude/create-bot-83gZr`, main file
   `src/app/streamlit_app.py`.
4. Click *Deploy*. You get a URL like
   `https://<your-name>-trad-bot.streamlit.app`.

The DB lives on the platform's ephemeral filesystem, so a redeploy
resets the paper portfolio — that's usually fine since you can recreate
state from history. **Do not** put `SIMPLE_BOT_LIVE=true` here unless
you have a way to lock down access; Streamlit Cloud apps are public by
default.

---

## Suggested validation workflow

1. **Day 0**: run `python -m scripts.backtest_trend --years 5`. If the
   strategy materially underperforms buy-and-hold or has unacceptable
   drawdowns, stop here — tune `--sma` or change strategies.
2. **Days 1–14**: run local paper trading. Click *Evaluate now* once a
   day. Confirm:
   - Signals fire when expected (compare to TradingView's BTC chart
     with a 50-day MA overlay).
   - Orders simulate correctly (balances move the right way).
   - The equity curve looks sane.
3. **Day 14**: review the paper equity curve vs. what buy-and-hold did
   over the same period. If you'd want to keep running it, proceed to
   live with €100–150 (per the spec's micro-live gate).
4. **Day 14+**: rotate paper keys are unnecessary — paper never had a
   key. To go live, follow `docs/OPERATIONS.md` §2 (live API key with
   no-withdrawal scope, IP-whitelisted to your VPS).
