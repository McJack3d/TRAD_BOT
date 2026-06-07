"""Does sentiment actually improve the SMA trend bot? — A/B harness.

Runs the SMA trend backtest at several sentiment weights (0 = the
SMA-only baseline) against the *same* price history and the *same*
sentiment series, and reports risk-adjusted metrics side by side. The
question this answers: does tilting the SMA thresholds by a sentiment
factor beat plain SMA, after fees, on a risk-adjusted basis?

IMPORTANT — what is and isn't backtestable here:

  * **Fear & Greed**: has a daily history back to 2018
    (`FearGreedSentiment.history()`), so it can be replayed with no
    lookahead. This harness validates it honestly.
  * **AI news sentiment** (`AiSentiment`): there is NO free historical
    archive of crypto headlines, so it CANNOT be replayed this way. It
    can only be validated *forward* (log the live factor daily and
    compare after N weeks). Do not infer the AI source's value from a
    Fear & Greed backtest — they are different signals. This module
    deliberately takes the sentiment series as an argument rather than
    pretending it can reconstruct AI history.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from src.backtest.trend_backtest import TrendBacktestResult, backtest_sma_trend


@dataclass(slots=True)
class SentimentABRow:
    label: str
    weight: float
    apr: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    final_equity: float


def _metrics(result: TrendBacktestResult) -> tuple[float, float, float]:
    """(apr, sharpe, max_drawdown) from a backtest result's daily curve."""
    eq = result.equity_curve
    if eq.empty:
        return 0.0, 0.0, 0.0
    e = eq.copy()
    e["ts"] = pd.to_datetime(e["ts"], utc=True)
    e = e.set_index("ts").sort_index()
    equity = e["strategy_equity"]

    span_days = (e.index[-1] - e.index[0]).total_seconds() / 86400 or 1.0
    start = float(result.initial_equity)
    end = float(result.final_equity)
    ret = (end / start - 1) if start > 0 else 0.0
    apr = (1 + ret) ** (365.0 / span_days) - 1 if ret > -1 else -1.0

    daily = equity.pct_change().dropna()
    sharpe = (
        float(daily.mean() / daily.std(ddof=0) * math.sqrt(365))
        if not daily.empty and daily.std(ddof=0) > 0
        else 0.0
    )

    peaks = equity.cummax()
    max_dd = float((equity / peaks - 1).min())
    return apr, sharpe, max_dd


def compare_sentiment_weights(
    daily_closes: pd.Series,
    sentiment_series: pd.Series,
    weights: tuple[float, ...] = (0.0, 0.01, 0.03, 0.05),
    *,
    sma_window: int = 200,
    fee_bps: Decimal = Decimal("4.0"),
    slippage_bps: Decimal = Decimal("2.0"),
    entry_buffer_pct: float = 0.01,
    exit_buffer_pct: float = 0.01,
    initial_equity: Decimal = Decimal("1000"),
) -> list[SentimentABRow]:
    """Backtest each weight on the same closes + sentiment series.

    Weight 0 is the SMA-only baseline (sentiment series ignored). Higher
    weights let the factor tilt the entry/exit thresholds more.
    """
    rows: list[SentimentABRow] = []
    for w in weights:
        result = backtest_sma_trend(
            daily_closes,
            initial_equity=initial_equity,
            sma_window=sma_window,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            entry_buffer_pct=entry_buffer_pct,
            exit_buffer_pct=exit_buffer_pct,
            sentiment_series=None if w == 0 else sentiment_series,
            sentiment_weight=w,
        )
        apr, sharpe, max_dd = _metrics(result)
        rows.append(
            SentimentABRow(
                label="SMA-only (baseline)" if w == 0 else f"SMA + sentiment w={w:.2f}",
                weight=w,
                apr=apr,
                sharpe=sharpe,
                max_drawdown=max_dd,
                n_trades=len(result.trades),
                final_equity=float(result.final_equity),
            )
        )
    return rows


def verdict(rows: list[SentimentABRow]) -> str:
    """Plain-English read: did any weighted variant beat the baseline on
    Sharpe without materially worse drawdown?"""
    base = next((r for r in rows if r.weight == 0), None)
    weighted = [r for r in rows if r.weight != 0]
    if base is None or not weighted:
        return "Need a baseline (weight 0) and at least one weighted run."
    best = max(weighted, key=lambda r: r.sharpe)
    d_sharpe = best.sharpe - base.sharpe
    d_dd = best.max_drawdown - base.max_drawdown  # less-negative is better
    if d_sharpe <= 0.05:
        return (
            f"Sentiment does NOT help: best weighted Sharpe {best.sharpe:.2f} "
            f"vs baseline {base.sharpe:.2f} (Δ{d_sharpe:+.2f}). Keep it off."
        )
    if d_dd < -0.05:
        return (
            f"Mixed: best weighted Sharpe improves (Δ{d_sharpe:+.2f}) but max "
            f"drawdown worsens by {-d_dd:.1%}. Not a clear win."
        )
    return (
        f"Sentiment helps at w={best.weight:.2f}: Sharpe {best.sharpe:.2f} vs "
        f"baseline {base.sharpe:.2f} (Δ{d_sharpe:+.2f}), drawdown not worse. "
        f"Worth a forward paper test before trusting capital to it."
    )
