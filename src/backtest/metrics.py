"""Performance metrics for backtest results.

All metrics computed in plain Python / numpy on the equity curve so the
backtester stays dependency-light.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.backtest.engine import BacktestResult


@dataclass
class Metrics:
    net_apr: float
    sharpe: float
    max_drawdown: float
    time_in_market_pct: float
    avg_dwell_hours: float
    n_trades: int
    win_rate: float
    avg_pnl_per_trade: float
    initial_equity: float
    final_equity: float


def compute_metrics(result: BacktestResult) -> Metrics:
    if result.equity_curve.empty:
        return Metrics(
            net_apr=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            time_in_market_pct=0.0,
            avg_dwell_hours=0.0,
            n_trades=0,
            win_rate=0.0,
            avg_pnl_per_trade=0.0,
            initial_equity=float(result.initial_equity),
            final_equity=float(result.final_equity),
        )

    eq = result.equity_curve.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    eq = eq.set_index("ts").sort_index()
    span_days = (eq.index[-1] - eq.index[0]).total_seconds() / 86400 or 1.0
    initial = float(result.initial_equity)
    final = float(result.final_equity)
    total_return = (final / initial) - 1 if initial > 0 else 0.0
    net_apr = (1 + total_return) ** (365.0 / span_days) - 1 if total_return > -1 else -1.0

    daily = eq["equity"].resample("1D").last().ffill()
    daily_ret = daily.pct_change().dropna()
    sharpe = 0.0
    if not daily_ret.empty and daily_ret.std() > 0:
        sharpe = float(np.sqrt(365) * daily_ret.mean() / daily_ret.std())

    running_max = eq["equity"].cummax()
    drawdown = eq["equity"] / running_max - 1.0
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0

    n_trades = len(result.trades)
    if n_trades:
        dwell_hours = [
            (t.exit_ts - t.entry_ts).total_seconds() / 3600 for t in result.trades
        ]
        avg_dwell = float(np.mean(dwell_hours))
        wins = sum(1 for t in result.trades if t.net_pnl > 0)
        win_rate = wins / n_trades
        avg_pnl = float(np.mean([float(t.net_pnl) for t in result.trades]))
        total_dwell_seconds = sum(
            (t.exit_ts - t.entry_ts).total_seconds() for t in result.trades
        )
        time_in_market_pct = total_dwell_seconds / (span_days * 86400)
    else:
        avg_dwell = 0.0
        win_rate = 0.0
        avg_pnl = 0.0
        time_in_market_pct = 0.0

    return Metrics(
        net_apr=net_apr,
        sharpe=sharpe,
        max_drawdown=max_dd,
        time_in_market_pct=time_in_market_pct,
        avg_dwell_hours=avg_dwell,
        n_trades=n_trades,
        win_rate=win_rate,
        avg_pnl_per_trade=avg_pnl,
        initial_equity=initial,
        final_equity=final,
    )
