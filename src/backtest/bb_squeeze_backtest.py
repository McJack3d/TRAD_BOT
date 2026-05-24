"""Backtest the BB-squeeze + RSI mean-reversion strategy on intraday bars.

Bar-by-bar simulator that drives the SqueezeState machine identically
to how the live bot would. Each round trip is recorded with entry/exit
prices, duration, and PnL so we can characterize the strategy honestly
(win rate, average win/loss, max drawdown — not just APR).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from src.strategy.bb_squeeze import (
    SqueezeAction,
    SqueezeParams,
    SqueezeState,
    evaluate_at,
    precompute_squeeze,
)


@dataclass
class SqueezeBacktestResult:
    equity_curve: pd.DataFrame  # ts, close, strategy_equity, buy_and_hold_equity, position
    trades: list[dict] = field(default_factory=list)  # one row PER round trip
    initial_equity: Decimal = Decimal("0")
    final_equity: Decimal = Decimal("0")
    final_buy_and_hold: Decimal = Decimal("0")


def _build_trend_lookup(
    daily_closes: pd.Series | None,
    sma_window: int,
    entry_buffer_pct: float,
) -> tuple[pd.Series, pd.Series] | None:
    """Pre-compute the daily SMA and a series of effective entry thresholds.

    Returns (daily_close_series, threshold_series) both UTC-indexed and
    sorted, so the caller can do a cheap `.asof(ts)` lookup per bar.
    """
    if daily_closes is None or daily_closes.empty:
        return None
    s = daily_closes.copy()
    s.index = pd.to_datetime(s.index, utc=True)
    s = s.sort_index()
    sma = s.rolling(sma_window).mean()
    thresh = sma * (1.0 + entry_buffer_pct)
    return s, thresh


def backtest_bb_squeeze(
    closes: pd.Series,
    initial_equity: Decimal = Decimal("1000"),
    fee_bps: Decimal = Decimal("4.0"),
    slippage_bps: Decimal = Decimal("2.0"),
    params: SqueezeParams | None = None,
    daily_closes: pd.Series | None = None,
    trend_sma_window: int = 200,
    trend_entry_buffer_pct: float = 0.01,
) -> SqueezeBacktestResult:
    """Run the BB-squeeze strategy on a series of intraday closes.

    `closes` is a DatetimeIndex-keyed Series of float closes (5m bars
    by convention, but the strategy doesn't care about the timeframe).
    Trades the full equity on each entry — no position sizing inside
    the backtest. Fees + slippage are deducted from both sides.

    `daily_closes` (optional) enables a daily-SMA trend regime filter:
    at each intraday bar we look up the most recent daily close and
    its SMA(trend_sma_window). If `daily_close > sma * (1 + buffer)`
    the regime is "up" and entries are allowed; otherwise entries are
    blocked. Exits are never blocked. The daily series must extend
    earlier than `closes.index[0]` by at least `trend_sma_window` days
    for the filter to fire at the start of the test window.
    """
    p = params or SqueezeParams()
    cost_factor = Decimal("1") - (fee_bps + slippage_bps) / Decimal("10000")
    trend_lookup = _build_trend_lookup(
        daily_closes, trend_sma_window, trend_entry_buffer_pct
    )
    # ONE-TIME vectorized indicator compute — was the O(n²) bottleneck.
    pre = precompute_squeeze(closes, p)

    equity = initial_equity
    qty = Decimal("0")
    state = SqueezeState.FLAT
    armed_at_index: int | None = None
    entry_bar_index: int | None = None
    entry_price: Decimal | None = None
    entry_ts: pd.Timestamp | None = None

    rows: list[dict] = []
    trades: list[dict] = []
    initial_close = Decimal(str(closes.iloc[0]))

    def _trend_up_asof(ts: pd.Timestamp) -> bool:
        # No filter configured → always allow (preserves prior behavior).
        if trend_lookup is None:
            return True
        daily_series, threshold_series = trend_lookup
        # asof returns NaN if no prior value exists or the SMA hasn't warmed
        # up yet. In that case we conservatively disallow entries — better
        # to miss trades than make them blind.
        ts_utc = ts if ts.tzinfo else pd.Timestamp(ts, tz="UTC")
        d_close = daily_series.asof(ts_utc)
        d_thresh = threshold_series.asof(ts_utc)
        if pd.isna(d_close) or pd.isna(d_thresh):
            return False
        return float(d_close) > float(d_thresh)

    for i in range(len(closes)):
        ts = closes.index[i]
        close = Decimal(str(closes.iloc[i]))
        signal = evaluate_at(
            closes, pre, i,
            state=state,
            armed_at_index=armed_at_index,
            params=p,
            trend_up=_trend_up_asof(ts),
        )

        if signal.action == SqueezeAction.ARM:
            armed_at_index = i
            state = SqueezeState.ARMED
        elif signal.action == SqueezeAction.DISARM:
            armed_at_index = None
            state = SqueezeState.FLAT
        elif signal.action == SqueezeAction.BUY:
            spend = equity * cost_factor
            qty = spend / close
            equity = Decimal("0")
            state = SqueezeState.LONG
            entry_bar_index = i
            entry_price = close
            entry_ts = ts
            armed_at_index = None
        elif signal.action == SqueezeAction.SELL:
            proceeds = qty * close * cost_factor
            assert entry_price is not None and entry_ts is not None
            pnl = proceeds - (qty * entry_price)
            trades.append(
                {
                    "entry_ts": entry_ts,
                    "exit_ts": ts,
                    "entry_price": float(entry_price),
                    "exit_price": float(close),
                    "qty": float(qty),
                    "pnl": float(pnl),
                    "return_pct": float((close / entry_price) - Decimal("1")),
                    "bars_held": i - (entry_bar_index or i),
                    "exit_reason": signal.reason,
                }
            )
            equity = proceeds
            qty = Decimal("0")
            state = SqueezeState.FLAT
            entry_bar_index = None
            entry_price = None
            entry_ts = None

        mark_to_market = equity + qty * close
        buy_and_hold = initial_equity * (close / initial_close)
        rows.append(
            {
                "ts": ts,
                "close": float(close),
                "strategy_equity": float(mark_to_market),
                "buy_and_hold_equity": float(buy_and_hold),
                "position": state.value,
            }
        )

    curve = pd.DataFrame(rows)
    final_eq = Decimal(str(curve["strategy_equity"].iloc[-1])) if not curve.empty else initial_equity
    final_bh = Decimal(str(curve["buy_and_hold_equity"].iloc[-1])) if not curve.empty else initial_equity
    return SqueezeBacktestResult(
        equity_curve=curve,
        trades=trades,
        initial_equity=initial_equity,
        final_equity=final_eq,
        final_buy_and_hold=final_bh,
    )


def summarize(result: SqueezeBacktestResult) -> dict:
    """Compact stats. Includes win-rate and avg trade — critical for a
    high-frequency strategy where APR alone can hide a bad edge."""
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

    wins = [t for t in result.trades if t["pnl"] > 0]
    losses = [t for t in result.trades if t["pnl"] <= 0]
    avg_win = sum(t["return_pct"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["return_pct"] for t in losses) / len(losses) if losses else 0.0
    avg_bars = (
        sum(t["bars_held"] for t in result.trades) / len(result.trades)
        if result.trades else 0.0
    )

    return {
        "span_days": int(span_days),
        "n_trades": len(result.trades),
        "win_rate": (len(wins) / len(result.trades)) if result.trades else 0.0,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "avg_bars_held": avg_bars,
        "strategy_apr": _apr(result.initial_equity, result.final_equity),
        "buy_and_hold_apr": _apr(result.initial_equity, result.final_buy_and_hold),
        "strategy_final": float(result.final_equity),
        "buy_and_hold_final": float(result.final_buy_and_hold),
        "strategy_max_dd": _max_dd(strat),
        "buy_and_hold_max_dd": _max_dd(bh),
    }
