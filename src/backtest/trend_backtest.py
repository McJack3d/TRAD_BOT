"""Backtest the SMA trend-following strategy on real history.

Single-asset (`backtest_sma_trend`) and multi-asset basket
(`backtest_basket`) variants. Each asset has its own signal; the basket
equal-weights capital across assets that are IN at any moment.
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
    sma_window: int = 200,
    fee_bps: Decimal = Decimal("4.0"),
    slippage_bps: Decimal = Decimal("2.0"),
    entry_buffer_pct: float = 0.0,
    exit_buffer_pct: float = 0.0,
    trailing_stop_pct: float = 0.0,
) -> TrendBacktestResult:
    """Run the SMA trend strategy on a series of daily closes.

    `daily_closes` is a DatetimeIndex-keyed Series of float closes.
    Returns equity-curve DataFrame + trade list + summary numbers.

    `trailing_stop_pct` (default 0 = off) sets a peak-to-current trailing
    stop. When in a position, we track the highest close since entry; if
    today's close drops by more than `trailing_stop_pct` from that peak,
    we force-exit even if the SMA signal still says IN. Re-entry requires
    a fresh IN signal — this avoids ping-ponging on a single drawdown.
    """
    cost_bps = fee_bps + slippage_bps
    equity = initial_equity
    btc = Decimal("0")
    position = TrendState.OUT
    peak_since_entry: Decimal | None = None
    stopped_out_cooldown = False  # set after a trailing-stop sell
    rows: list[dict] = []
    trades: list[dict] = []
    initial_close = Decimal(str(daily_closes.iloc[0]))

    def _enter(close: Decimal, ts) -> None:
        nonlocal equity, btc, position, peak_since_entry
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
        position = TrendState.IN
        peak_since_entry = close

    def _exit(close: Decimal, ts, reason: str = "signal") -> None:
        nonlocal equity, btc, position, peak_since_entry
        proceeds = btc * close * (Decimal("1") - cost_bps / Decimal("10000"))
        trades.append(
            {
                "ts": ts,
                "side": "sell",
                "price": float(close),
                "qty": float(btc),
                "equity_after": float(proceeds),
                "reason": reason,
            }
        )
        equity = proceeds
        btc = Decimal("0")
        position = TrendState.OUT
        peak_since_entry = None

    for i in range(len(daily_closes)):
        ts = daily_closes.index[i]
        close = Decimal(str(daily_closes.iloc[i]))

        # Track peak for trailing stop and check stop condition first.
        if position == TrendState.IN:
            assert peak_since_entry is not None
            if close > peak_since_entry:
                peak_since_entry = close
            if trailing_stop_pct > 0:
                trigger = peak_since_entry * (Decimal("1") - Decimal(str(trailing_stop_pct)))
                if close <= trigger:
                    _exit(close, ts, reason="trailing_stop")
                    stopped_out_cooldown = True

        # SMA signal evaluation (with hysteresis).
        if i + 1 >= sma_window:
            window_closes = daily_closes.iloc[: i + 1]
            signal = evaluate_trend(
                window_closes,
                sma_window=sma_window,
                entry_buffer_pct=entry_buffer_pct,
                exit_buffer_pct=exit_buffer_pct,
            )
            # Cooldown: after a trailing stop, we wait for OUT to clear
            # before considering re-entry. This prevents re-buying on the
            # same drawdown bar.
            if stopped_out_cooldown and signal.state == TrendState.OUT:
                stopped_out_cooldown = False
            if signal.state != position and not stopped_out_cooldown:
                if signal.state == TrendState.IN:
                    _enter(close, ts)
                else:
                    _exit(close, ts, reason="signal")

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


def backtest_basket(
    closes_by_symbol: dict[str, pd.Series],
    initial_equity: Decimal = Decimal("1000"),
    sma_window: int = 200,
    fee_bps: Decimal = Decimal("4.0"),
    slippage_bps: Decimal = Decimal("2.0"),
    entry_buffer_pct: float = 0.0,
    exit_buffer_pct: float = 0.0,
) -> TrendBacktestResult:
    """Equal-weight basket backtest.

    Each symbol runs its own SMA-trend signal. Capital is split into
    n_symbols equal slots; a slot is invested when its symbol's signal
    is IN, and held as USDT otherwise.

    Index alignment: closes are reindexed onto the union of all
    timestamps and forward-filled, so a missing day for one symbol uses
    the previous close.

    Buy-and-hold benchmark = equal-weight buy of all symbols on day 0
    with rebalancing only at the end (no intermediate rebalances).
    """
    if not closes_by_symbol:
        return TrendBacktestResult(
            equity_curve=pd.DataFrame(columns=["ts", "strategy_equity", "buy_and_hold_equity"]),
            initial_equity=initial_equity,
            final_equity=initial_equity,
            final_buy_and_hold=initial_equity,
        )

    cost_bps = fee_bps + slippage_bps
    cost_factor = Decimal("1") - cost_bps / Decimal("10000")
    n = len(closes_by_symbol)
    slot_equity = initial_equity / Decimal(n)

    # Align all series on a union DatetimeIndex.
    idx = sorted(set().union(*(s.index for s in closes_by_symbol.values())))
    aligned = {
        sym: s.reindex(idx).ffill() for sym, s in closes_by_symbol.items()
    }
    symbols = list(closes_by_symbol.keys())

    cash = {sym: slot_equity for sym in symbols}
    holdings = {sym: Decimal("0") for sym in symbols}
    position = {sym: TrendState.OUT for sym in symbols}
    initial_close = {sym: Decimal(str(aligned[sym].iloc[0])) for sym in symbols}

    trades: list[dict] = []
    rows: list[dict] = []

    for i, ts in enumerate(idx):
        for sym in symbols:
            close = Decimal(str(aligned[sym].iloc[i]))
            if i + 1 >= sma_window:
                window = aligned[sym].iloc[: i + 1]
                signal = evaluate_trend(
                    window,
                    sma_window=sma_window,
                    entry_buffer_pct=entry_buffer_pct,
                    exit_buffer_pct=exit_buffer_pct,
                )
                if signal.state != position[sym]:
                    if signal.state == TrendState.IN:
                        spend = cash[sym] * cost_factor
                        qty = spend / close
                        trades.append(
                            {"ts": ts, "symbol": sym, "side": "buy", "price": float(close), "qty": float(qty)}
                        )
                        holdings[sym] = qty
                        cash[sym] = Decimal("0")
                    else:
                        proceeds = holdings[sym] * close * cost_factor
                        trades.append(
                            {"ts": ts, "symbol": sym, "side": "sell", "price": float(close), "qty": float(holdings[sym])}
                        )
                        cash[sym] = proceeds
                        holdings[sym] = Decimal("0")
                    position[sym] = signal.state

        strategy_equity = sum(
            cash[sym] + holdings[sym] * Decimal(str(aligned[sym].iloc[i])) for sym in symbols
        )
        buy_and_hold = sum(
            slot_equity * Decimal(str(aligned[sym].iloc[i])) / initial_close[sym] for sym in symbols
        )
        rows.append(
            {
                "ts": ts,
                "strategy_equity": float(strategy_equity),
                "buy_and_hold_equity": float(buy_and_hold),
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
