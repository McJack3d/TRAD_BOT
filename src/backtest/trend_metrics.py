"""Expanded metrics for trend-strategy evaluation.

Beyond APR + max drawdown:
- Sharpe: return per unit of volatility, annualized.
- Sortino: same but only penalizes downside volatility (better for
  asymmetric returns).
- Calmar: APR / max drawdown (the higher the better; >1 is excellent).
- Ulcer Index: RMS of drawdown depths. Penalizes prolonged or repeated
  drawdowns more than a single sharp one.
- Win rate / avg win / avg loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd

from src.backtest.trend_backtest import TrendBacktestResult


@dataclass
class FullMetrics:
    initial_equity: float
    final_equity: float
    net_apr: float
    sharpe: float
    sortino: float
    calmar: float
    ulcer_index: float
    max_drawdown: float
    n_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    span_days: int
    buy_and_hold_apr: float
    buy_and_hold_max_dd: float
    buy_and_hold_sharpe: float


def compute_full_metrics(result: TrendBacktestResult) -> FullMetrics:
    if result.equity_curve.empty:
        return FullMetrics(
            initial_equity=float(result.initial_equity),
            final_equity=float(result.final_equity),
            net_apr=0.0,
            sharpe=0.0,
            sortino=0.0,
            calmar=0.0,
            ulcer_index=0.0,
            max_drawdown=0.0,
            n_trades=0,
            win_rate=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            span_days=0,
            buy_and_hold_apr=0.0,
            buy_and_hold_max_dd=0.0,
            buy_and_hold_sharpe=0.0,
        )

    eq = result.equity_curve.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    eq = eq.set_index("ts").sort_index()
    span_days = max(1, int((eq.index[-1] - eq.index[0]).total_seconds() / 86400))

    strat = eq["strategy_equity"]
    bh = eq["buy_and_hold_equity"]

    return FullMetrics(
        initial_equity=float(result.initial_equity),
        final_equity=float(result.final_equity),
        net_apr=_apr(result.initial_equity, result.final_equity, span_days),
        sharpe=_sharpe(strat),
        sortino=_sortino(strat),
        calmar=_calmar(strat, span_days),
        ulcer_index=_ulcer_index(strat),
        max_drawdown=_max_drawdown(strat),
        n_trades=len(result.trades),
        win_rate=_win_rate(result.trades),
        avg_win=_avg_pnl(result.trades, winners=True),
        avg_loss=_avg_pnl(result.trades, winners=False),
        span_days=span_days,
        buy_and_hold_apr=_apr(result.initial_equity, result.final_buy_and_hold, span_days),
        buy_and_hold_max_dd=_max_drawdown(bh),
        buy_and_hold_sharpe=_sharpe(bh),
    )


def _apr(start: Decimal, end: Decimal, span_days: int) -> float:
    if start <= 0 or span_days <= 0:
        return 0.0
    ret = float(end) / float(start) - 1
    if ret <= -1:
        return -1.0
    return (1 + ret) ** (365.0 / span_days) - 1


def _daily_returns(equity: pd.Series) -> pd.Series:
    daily = equity.resample("1D").last().ffill()
    return daily.pct_change().dropna()


def _sharpe(equity: pd.Series, annualization: float = 365.0) -> float:
    rets = _daily_returns(equity)
    if rets.empty or rets.std() == 0:
        return 0.0
    return float(np.sqrt(annualization) * rets.mean() / rets.std())


def _sortino(equity: pd.Series, annualization: float = 365.0) -> float:
    rets = _daily_returns(equity)
    if rets.empty:
        return 0.0
    downside = rets[rets < 0]
    ds_std = downside.std()
    # downside.std() is NaN if downside has < 2 elements (ddof=1). Treat
    # those cases as "no meaningful downside" and fall back to Sharpe.
    if downside.empty or np.isnan(ds_std) or ds_std == 0:
        if rets.std() == 0:
            return 0.0
        return float(np.sqrt(annualization) * rets.mean() / rets.std())
    return float(np.sqrt(annualization) * rets.mean() / ds_std)


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peaks = equity.cummax()
    return float((equity / peaks - 1).min())


def _calmar(equity: pd.Series, span_days: int) -> float:
    if equity.empty or span_days <= 0:
        return 0.0
    apr = _apr(Decimal(str(equity.iloc[0])), Decimal(str(equity.iloc[-1])), span_days)
    dd = _max_drawdown(equity)
    if dd == 0:
        return 0.0
    return apr / abs(dd)


def _ulcer_index(equity: pd.Series) -> float:
    """RMS of drawdown depths. 0 = no drawdown ever; higher = bumpy."""
    if equity.empty:
        return 0.0
    peaks = equity.cummax()
    dd_pct = (equity / peaks - 1) * 100
    return float(np.sqrt((dd_pct**2).mean()))


def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    # A "win" here is a sell whose proceeds exceed the matching buy's spend.
    # We approximate by pairing buys and sells in order.
    realized_pnls: list[float] = []
    last_buy_price: float | None = None
    last_buy_qty: float | None = None
    for t in trades:
        if t["side"] == "buy":
            last_buy_price = t["price"]
            last_buy_qty = t["qty"]
        elif t["side"] == "sell" and last_buy_price is not None:
            pnl = (t["price"] - last_buy_price) * (last_buy_qty or t["qty"])
            realized_pnls.append(pnl)
            last_buy_price = None
            last_buy_qty = None
    if not realized_pnls:
        return 0.0
    return sum(1 for p in realized_pnls if p > 0) / len(realized_pnls)


def _avg_pnl(trades: list[dict], winners: bool) -> float:
    realized_pnls: list[float] = []
    last_buy_price: float | None = None
    last_buy_qty: float | None = None
    for t in trades:
        if t["side"] == "buy":
            last_buy_price = t["price"]
            last_buy_qty = t["qty"]
        elif t["side"] == "sell" and last_buy_price is not None:
            pnl = (t["price"] - last_buy_price) * (last_buy_qty or t["qty"])
            realized_pnls.append(pnl)
            last_buy_price = None
            last_buy_qty = None
    sel = [p for p in realized_pnls if (p > 0) == winners]
    return float(np.mean(sel)) if sel else 0.0
