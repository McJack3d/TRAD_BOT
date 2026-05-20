"""Backtest the SMA trend-following strategy on real BTC history.

Single-asset, spot-only. For each daily close, compute the signal;
if it differs from the current position, flip with a configurable
fee + slippage cost. Reports the strategy's equity curve alongside
a buy-and-hold benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from src.strategy.sma_trend import TrendState, evaluate_trend


@dataclass
class TrendBacktestResult:
    equity_curve: pd.DataFrame  # ts, strategy_equity, buy_and_hold_equity, position
    trades: list[dict] = field(default_factory=list)
    initial_equity: Decimal = Decimal("0")
    final_equity: Decimal = Decimal("0")
    final_buy_and_hold: Decimal = Decimal("0")


def backtest_sma_trend(
    daily_closes: pd.Series,
    initial_equity: Decimal = Decimal("1000"),
    sma_window: int = 50,
    fee_bps: Decimal = Decimal("4.0"),
    slippage_bps: Decimal = Decimal("2.0"),
) -> TrendBacktestResult:
    """Run the SMA trend strategy on a series of daily closes.

    `daily_closes` is a DatetimeIndex-keyed Series of float closes.
    Returns equity-curve DataFrame + trade list + summary numbers.
    """
    cost_bps = fee_bps + slippage_bps
    equity = initial_equity
    btc = Decimal("0")
    position = TrendState.OUT
    rows: list[dict] = []
    trades: list[dict] = []
    initial_close = Decimal(str(daily_closes.iloc[0]))

    for i in range(len(daily_closes)):
        ts = daily_closes.index[i]
        close = Decimal(str(daily_closes.iloc[i]))

        # Need at least sma_window history to evaluate.
        if i + 1 >= sma_window:
            window_closes = daily_closes.iloc[: i + 1]
            signal = evaluate_trend(window_closes, sma_window=sma_window)
            if signal.state != position:
                if signal.state == TrendState.IN:
                    # Buy BTC with all USDT.
                    spend = equity * (Decimal("1") - cost_bps / Decimal("10000"))
                    btc_acquired = spend / close
                    trades.append(
                        {
                            "ts": ts,
                            "side": "buy",
                            "price": float(close),
                            "qty": float(btc_acquired),
                            "equity_before": float(equity),
                        }
                    )
                    btc = btc_acquired
                    equity = Decimal("0")
                else:
                    # Sell all BTC to USDT.
                    proceeds = btc * close * (Decimal("1") - cost_bps / Decimal("10000"))
                    trades.append(
                        {
                            "ts": ts,
                            "side": "sell",
                            "price": float(close),
                            "qty": float(btc),
                            "equity_after": float(proceeds),
                        }
                    )
                    equity = proceeds
                    btc = Decimal("0")
                position = signal.state

        mark_to_market = equity + btc * close
        buy_and_hold = initial_equity * (close / initial_close)
        rows.append(
            {
                "ts": ts,
                "close": float(close),
                "strategy_equity": float(mark_to_market),
                "buy_and_hold_equity": float(buy_and_hold),
                "position": position.value,
            }
        )

    curve = pd.DataFrame(rows)
    final_eq = Decimal(str(curve["strategy_equity"].iloc[-1])) if not curve.empty else initial_equity
    final_bh = Decimal(str(curve["buy_and_hold_equity"].iloc[-1])) if not curve.empty else initial_equity
    return TrendBacktestResult(
        equity_curve=curve,
        trades=trades,
        initial_equity=initial_equity,
        final_equity=final_eq,
        final_buy_and_hold=final_bh,
    )


def summarize(result: TrendBacktestResult) -> dict:
    """Compact stats for display."""
    if result.equity_curve.empty:
        return {}
    eq = result.equity_curve.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    eq = eq.set_index("ts").sort_index()
    span_days = (eq.index[-1] - eq.index[0]).total_seconds() / 86400 or 1.0

    def _apr(start: Decimal, end: Decimal) -> float:
        ret = float(end / start) - 1 if start > 0 else 0.0
        return (1 + ret) ** (365.0 / span_days) - 1 if ret > -1 else -1.0

    def _max_dd(s: pd.Series) -> float:
        peaks = s.cummax()
        return float((s / peaks - 1).min())

    strat = eq["strategy_equity"]
    bh = eq["buy_and_hold_equity"]
    return {
        "span_days": int(span_days),
        "n_trades": len(result.trades),
        "strategy_apr": _apr(result.initial_equity, result.final_equity),
        "buy_and_hold_apr": _apr(result.initial_equity, result.final_buy_and_hold),
        "strategy_final": float(result.final_equity),
        "buy_and_hold_final": float(result.final_buy_and_hold),
        "strategy_max_dd": _max_dd(strat),
        "buy_and_hold_max_dd": _max_dd(bh),
    }
