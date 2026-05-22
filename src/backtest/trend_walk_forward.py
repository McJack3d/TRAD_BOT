"""Walk-forward validation for the SMA trend strategy.

Pattern: rolling train / test windows. Tune (sma_window, entry_buffer)
on the train window for best Sharpe; then evaluate the chosen params
on the immediately following test window. Repeat.

Spec §8.4 anti-overfit rules apply:
- At most 3 free parameters (we tune 2: sma_window, entry_buffer).
- Coarse grids (no 3-decimal precision).
- Out-of-sample evaluation only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from itertools import product

import pandas as pd

from src.backtest.trend_backtest import TrendBacktestResult, backtest_sma_trend
from src.backtest.trend_metrics import FullMetrics, compute_full_metrics


def _slice_to_test_window(
    r: TrendBacktestResult,
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
    baseline_equity: Decimal,
) -> TrendBacktestResult:
    """Trim a full backtest result (warmup + test) down to just the test
    portion.

    Strategy equity is left as-is (it was already `baseline_equity` at
    test_start because the bot was idle during warmup). Buy-and-hold is
    REBASED so its value at test_start equals `baseline_equity` —
    otherwise we'd be crediting the warmup-period BTC move to the test
    window's B&H number, which would overstate the benchmark.
    """
    eq = r.equity_curve.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    eq = eq[(eq["ts"] >= test_start) & (eq["ts"] <= test_end)].reset_index(drop=True)

    def _trade_in_window(t: dict) -> bool:
        ts = pd.Timestamp(t["ts"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return test_start <= ts <= test_end

    test_trades = [t for t in r.trades if _trade_in_window(t)]
    if eq.empty:
        return TrendBacktestResult(
            equity_curve=eq,
            trades=test_trades,
            initial_equity=baseline_equity,
            final_equity=baseline_equity,
            final_buy_and_hold=baseline_equity,
        )
    bh_at_test_start = eq["buy_and_hold_equity"].iloc[0]
    if bh_at_test_start > 0:
        scale = float(baseline_equity) / float(bh_at_test_start)
        eq["buy_and_hold_equity"] = eq["buy_and_hold_equity"] * scale
    final_eq = Decimal(str(eq["strategy_equity"].iloc[-1]))
    final_bh = Decimal(str(eq["buy_and_hold_equity"].iloc[-1]))
    return TrendBacktestResult(
        equity_curve=eq,
        trades=test_trades,
        initial_equity=baseline_equity,
        final_equity=final_eq,
        final_buy_and_hold=final_bh,
    )

# Coarse grids — no fine tuning. Anything beyond this is overfitting.
SMA_GRID = [100, 150, 200, 250]
BUFFER_GRID = [0.0, 0.005, 0.01, 0.02]


@dataclass
class WFWindow:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    best_sma: int
    best_buffer: float
    train_metrics: FullMetrics
    test_metrics: FullMetrics


def walk_forward_trend(
    daily_closes: pd.Series,
    train_days: int = 730,  # 2 years train
    test_days: int = 180,  # 6 months test
    initial_equity: Decimal = Decimal("1000"),
    trailing_stop_pct: float = 0.0,
) -> list[WFWindow]:
    """Roll train/test windows through the series and report each.

    Returns one WFWindow per stride. Each window's `test_metrics` is the
    honest out-of-sample number — that's what counts.
    """
    closes = daily_closes.sort_index()
    if closes.empty:
        return []
    start = closes.index[0]
    end = closes.index[-1]

    windows: list[WFWindow] = []
    cur = start
    while cur + timedelta(days=train_days + test_days) <= end:
        train_end = cur + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)
        train_closes = closes.loc[cur:train_end]
        test_closes = closes.loc[train_end:test_end]

        # Grid search on the train window for best Sharpe — but only among
        # parameter sets that actually traded. A no-trade run has zero
        # variance and looks deceptively "safe" to a naive Sharpe optimizer.
        best: tuple[FullMetrics, int, float, int] | None = None
        for sma, buffer in product(SMA_GRID, BUFFER_GRID):
            if len(train_closes) < sma + 30:
                continue
            r = backtest_sma_trend(
                train_closes,
                initial_equity=initial_equity,
                sma_window=sma,
                entry_buffer_pct=buffer,
                exit_buffer_pct=buffer,
                trailing_stop_pct=trailing_stop_pct,
            )
            if len(r.trades) < 2:
                # Need at least one round-trip to know the params do anything.
                continue
            m = compute_full_metrics(r)
            if best is None or m.sharpe > best[0].sharpe:
                best = (m, sma, buffer, len(r.trades))

        if best is None:
            cur += timedelta(days=test_days)
            continue

        train_metrics, sma, buffer, _ = best
        # Feed the test backtest enough pre-test history to compute SMA
        # immediately, then mark trade_start so it doesn't trade on the
        # warmup days. Otherwise an SMA-250 chosen by the optimizer can't
        # do anything on a 180-day test window — it'd never warm up.
        warmup_start = train_end - timedelta(days=sma + 30)
        test_with_warmup = closes.loc[warmup_start:test_end]
        full_r = backtest_sma_trend(
            test_with_warmup,
            initial_equity=initial_equity,
            sma_window=sma,
            entry_buffer_pct=buffer,
            exit_buffer_pct=buffer,
            trailing_stop_pct=trailing_stop_pct,
            trade_start=train_end,
        )
        test_r = _slice_to_test_window(full_r, train_end, test_end, initial_equity)
        test_metrics = compute_full_metrics(test_r)
        windows.append(
            WFWindow(
                train_start=cur,
                train_end=train_end,
                test_start=train_end,
                test_end=test_end,
                best_sma=sma,
                best_buffer=buffer,
                train_metrics=train_metrics,
                test_metrics=test_metrics,
            )
        )
        cur += timedelta(days=test_days)

    return windows


def oos_split(
    daily_closes: pd.Series,
    oos_fraction: float = 0.30,
) -> tuple[pd.Series, pd.Series]:
    """Split a time series into in-sample (older 70%) and out-of-sample
    (newest 30%). The OOS slice is never used for tuning — it's the
    final honest evaluation window."""
    n = len(daily_closes)
    split = int(n * (1 - oos_fraction))
    return daily_closes.iloc[:split], daily_closes.iloc[split:]
