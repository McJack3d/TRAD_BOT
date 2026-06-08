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
    trade_start: pd.Timestamp | None = None,
) -> list[SentimentABRow]:
    """Backtest each weight on the same closes + sentiment series.

    Weight 0 is the SMA-only baseline (sentiment series ignored). Higher
    weights let the factor tilt the entry/exit thresholds more.

    `trade_start` (optional) lets a caller feed extra leading closes to
    warm up the SMA without those bars producing trades — needed for
    short sub-windows where the window alone is shorter than `sma_window`.
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
            trade_start=trade_start,
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


# ---- multi-window analysis (regime-conditional comparison) -----------


@dataclass(slots=True)
class WindowSpec:
    """A pre-defined date window with a human-readable label and a
    direction tag (price-up / price-down / mixed) so the matrix readout
    is self-explaining."""

    name: str
    start: pd.Timestamp
    end: pd.Timestamp
    direction: str  # "up", "down", or "mixed"

    def describe(self) -> str:
        days = (self.end - self.start).days
        arrow = {"up": "↑", "down": "↓", "mixed": "↔"}.get(self.direction, "?")
        return (
            f"{self.name} {arrow}  {self.start.date()}→{self.end.date()} ({days}d)"
        )


def _direction(window_closes: pd.Series) -> str:
    if len(window_closes) < 2:
        return "mixed"
    start, end = float(window_closes.iloc[0]), float(window_closes.iloc[-1])
    if end > start * 1.10:
        return "up"
    if end < start * 0.90:
        return "down"
    return "mixed"


def detect_recent_windows(closes: pd.Series) -> list[WindowSpec]:
    """Detect pre-defined sub-windows from the price series:

      * full       — the entire input.
      * bull_to_ath — last cycle's low → ATH (the bull leg of the most
                     recent cycle). Uses a 540-day lookback before the ATH.
      * ath_to_bottom — ATH → lowest close after it (the bear leg). Only
                     emitted if there's at least 30 days after the ATH.
      * recent_12m — trailing 365 days.
      * recent_3m  — trailing 90 days (matches your "3 month forecast" ask).

    These are chosen up-front and reported together — no cherry-picking
    "which window looked best" after the fact. Each is tagged with its
    direction so the user can see in the readout whether sentiment helped
    in trending-up vs trending-down vs ranging conditions.
    """
    if closes.empty:
        return []
    closes = closes.sort_index()
    start, end = closes.index[0], closes.index[-1]

    windows: list[WindowSpec] = [
        WindowSpec("full", start, end, _direction(closes))
    ]

    # ATH + the legs around it.
    ath_idx = closes.idxmax()
    lookback_start = max(start, ath_idx - pd.Timedelta(days=540))
    pre_ath = closes.loc[lookback_start:ath_idx]
    if len(pre_ath) >= 30:
        bull_start = pre_ath.idxmin()
        if bull_start < ath_idx and (ath_idx - bull_start).days >= 30:
            seg = closes.loc[bull_start:ath_idx]
            windows.append(
                WindowSpec("bull_to_ath", bull_start, ath_idx, _direction(seg))
            )

    post_ath = closes.loc[ath_idx:]
    if len(post_ath) >= 30:
        bottom = post_ath.idxmin()
        if bottom > ath_idx and (bottom - ath_idx).days >= 30:
            seg = closes.loc[ath_idx:bottom]
            windows.append(
                WindowSpec("ath_to_bottom", ath_idx, bottom, _direction(seg))
            )

    for name, days in (("recent_12m", 365), ("recent_3m", 90)):
        win_start = end - pd.Timedelta(days=days)
        if win_start > start:
            seg = closes.loc[win_start:end]
            if len(seg) >= 30:
                windows.append(
                    WindowSpec(name, seg.index[0], end, _direction(seg))
                )

    return windows


def compare_across_windows(
    closes: pd.Series,
    sentiment_series: pd.Series,
    weights: tuple[float, ...] = (0.0, 0.01, 0.03, 0.05),
    windows: list[WindowSpec] | None = None,
    *,
    sma_window: int = 200,
    entry_buffer_pct: float = 0.01,
    exit_buffer_pct: float = 0.01,
    initial_equity: Decimal = Decimal("1000"),
) -> dict[str, list[SentimentABRow]]:
    """Run `compare_sentiment_weights` in each window. For windows shorter
    than `sma_window`, the SMA gets warmed up on the leading closes BEFORE
    the window's start (no trading on those), then trades are restricted
    to the window proper via `trade_start`.

    Returns {window_name: rows}. Caller is responsible for printing.
    """
    if windows is None:
        windows = detect_recent_windows(closes)

    closes = closes.sort_index()
    out: dict[str, list[SentimentABRow]] = {}
    warmup_days = sma_window + 30  # a little slack so the very first signal is well-defined
    for w in windows:
        warmup_start = w.start - pd.Timedelta(days=warmup_days)
        warmup_start = max(warmup_start, closes.index[0])
        window_closes = closes.loc[warmup_start : w.end]
        if len(window_closes) < 30:
            continue
        # For SUB-windows (start > series start), require enough
        # pre-window history to warm the SMA. The "full" window starts at
        # the data's first day and the SMA warms up inside it naturally.
        pre_warmup_days = (w.start - closes.index[0]).days
        if pre_warmup_days > 0 and pre_warmup_days < 30:
            continue
        out[w.name] = compare_sentiment_weights(
            window_closes,
            sentiment_series,
            weights=weights,
            sma_window=sma_window,
            entry_buffer_pct=entry_buffer_pct,
            exit_buffer_pct=exit_buffer_pct,
            initial_equity=initial_equity,
            trade_start=w.start,
        )
    return out


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
