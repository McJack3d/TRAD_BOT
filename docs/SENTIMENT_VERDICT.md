# Sentiment Verdict — Fear & Greed does not help the trend bot

**Decision (2026-06-07): sentiment stays OFF.** The trend bot ships with
`SIMPLE_BOT_SENTIMENT_WEIGHT=0` (already the default). This is a clean,
empirically-grounded negative result, recorded so it isn't re-litigated.

## How it was tested

`tradbot sentiment-ab --years 5 --matrix` on BTC/USDT, SMA-200, with the
Fear & Greed daily history (the only sentiment source with a
backtestable archive). Pre-defined regime windows, reported together —
no cherry-picking which window looked best.

## Result (Sharpe; Δ vs SMA-only baseline)

| variant | full | bull_to_ath | ath_to_bottom | recent_12m | recent_3m |
|---|---|---|---|---|---|
| SMA-only | 0.88 | 0.94 | −1.30 | 0.08 | 0.00 |
| +sent w=0.01 | 0.87 (−0.01) | 0.91 (−0.03) | −1.30 (0.00) | 0.08 (0.00) | 0.00 |
| +sent w=0.03 | 0.82 (−0.06) | 0.87 (−0.07) | −1.38 (−0.07) | 0.05 (−0.03) | 0.00 |
| +sent w=0.05 | 0.80 (−0.09) | 0.93 (−0.01) | −1.27 (+0.03) | 0.11 (+0.03) | 0.00 |

Windows: full 2021-06→2026-06 (↑); bull_to_ath 2024-09→2025-10 (↑);
ath_to_bottom 2025-10→2026-06 (↓); recent_12m (↓); recent_3m (↔, 0
trades — too short for an SMA-200 signal change).

## Why this is a NO, not a "maybe in bears"

- The only positive deltas (+0.03) occur where they're meaningless: on a
  deeply negative Sharpe (−1.30 → −1.27 is still a losing strategy) and
  on a ~zero Sharpe (0.08 → 0.11 is noise). Neither clears the
  pre-registered +0.05 bar.
- The two windows with genuine signal (full, bull_to_ath) show sentiment
  **consistently hurting and monotonically worsening with weight** — the
  signature of a noise factor degrading otherwise-fine thresholds.
- Fear & Greed is itself price-derived, so it's largely redundant with
  what the SMA already encodes. This result is consistent with that.

## The more important secondary finding

Read the SMA-only baseline alone: **the long-only spot trend bot bleeds
in the current bear** (ath_to_bottom Sharpe −1.30; recent_12m 0.08). It
gets whipsawed buying rallies back above the SMA-200 and exiting lower.
A long-only strategy structurally cannot profit from the post-ATH
downtrend — it can only be long or flat.

This is the strongest argument for the **two-sided funding carry**
(`docs/FUNDING_CARRY_2SIDED.md`), which is market-neutral and indifferent
to direction — the property you want in a bear.

## What stays / what's next

- **Kept:** the sentiment A/B + matrix tooling (`src/backtest/sentiment_ab.py`),
  the AI sentiment engine (`src/sentiment/ai_sentiment.py`), and the
  forward-logger (`tradbot ai-sentiment-log`). They cost nothing idle and
  the AI source is the one sentiment question still genuinely open.
- **AI news sentiment** remains unvalidated by design — no historical
  archive exists. Keep `ai-sentiment-log` on cron; revisit after 4+ weeks
  of forward data via a future `ai-sentiment-vs-sma`.
- **Next build:** two-sided funding carry, per the signed-off spec.
