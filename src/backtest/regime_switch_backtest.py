"""Bar-by-bar backtester for the regime-switching long/short strategy.

Drives the same `evaluate_at` state machine the live bot would, with a
realistic cost model:

  * **ATR position sizing** — risk `risk_per_trade_pct` of equity per
    trade, with size derived from the ATR stop distance, capped at
    `max_leverage`.
  * **Fees + slippage** on both legs of every round trip.
  * **Funding** debited/credited every 8h a position is held (longs pay
    positive funding, shorts receive it) when a funding series is given.
  * **Cool-off** — after a stopped-out loss, no re-entry for
    `cooloff_bars` bars (the kill-switch layer cheap enough to model
    here; per-asset and consecutive-loss breakers are portfolio/live
    concerns applied later).

One symbol per run; the CLI runs BTC and ETH separately. Equity is
marked to market every bar so the curve (and Sharpe / drawdown) reflect
open-position risk, not just realized PnL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.strategy.regime import Regime
from src.strategy.regime_switch import (
    Action,
    RegimeSwitchParams,
    SwitchPosition,
    evaluate_at,
    open_from_signal,
    precompute,
)


@dataclass
class RegimeBacktestResult:
    equity_curve: pd.DataFrame  # ts, close, equity, position, regime
    trades: list[dict] = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0
    final_buy_and_hold: float = 0.0
    funding_applied: bool = False


def _funding_between(
    funding: pd.Series | None,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    notional: float,
    side: int,
) -> float:
    """PnL from funding over (entry_ts, exit_ts]. Longs pay positive
    funding (negative PnL); shorts receive it. `funding` is a
    ts-indexed Series of per-interval rates."""
    if funding is None or funding.empty:
        return 0.0
    window = funding[(funding.index > entry_ts) & (funding.index <= exit_ts)]
    if window.empty:
        return 0.0
    return float(-side * window.sum() * notional)


def backtest_regime_switch(
    df: pd.DataFrame,
    params: RegimeSwitchParams | None = None,
    initial_equity: float = 1000.0,
    fee_bps: float = 4.0,
    slippage_bps: float = 2.0,
    risk_per_trade_pct: float = 0.01,
    max_leverage: float = 3.0,
    cooloff_bars: int = 6,
    funding: pd.Series | None = None,
) -> RegimeBacktestResult:
    """Run the strategy over `df` (DatetimeIndex, columns high/low/close)."""
    p = params or RegimeSwitchParams()
    pre = precompute(df, p)
    n = len(df)
    fee = fee_bps / 10_000.0
    slip = slippage_bps / 10_000.0

    equity = float(initial_equity)
    pos = SwitchPosition.flat()
    cooloff_until = -1
    initial_close = float(pre.close[0])

    rows: list[dict] = []
    trades: list[dict] = []

    def _record_trade(exit_i: int, exit_fill: float, reason: str) -> float:
        """Close `pos`, return realized PnL (also mutates equity caller-side)."""
        notional_entry = pos.qty * pos.entry_price
        if pos.side == 1:
            gross = pos.qty * (exit_fill - pos.entry_price)
        else:
            gross = pos.qty * (pos.entry_price - exit_fill)
        exit_fee = pos.qty * exit_fill * fee
        fund = _funding_between(
            funding, df.index[pos.entry_index], df.index[exit_i], notional_entry, pos.side
        )
        net = gross - exit_fee + fund
        trades.append(
            {
                "entry_ts": df.index[pos.entry_index],
                "exit_ts": df.index[exit_i],
                "side": "long" if pos.side == 1 else "short",
                "leg": pos.entry_leg.value if pos.entry_leg else "",
                "entry_price": pos.entry_price,
                "exit_price": exit_fill,
                "qty": pos.qty,
                "equity_at_entry": pos.entry_equity,
                "gross_pnl": gross,
                "funding_pnl": fund,
                "net_pnl": net,
                "return_pct": (net / notional_entry) if notional_entry else 0.0,
                "bars_held": exit_i - pos.entry_index,
                "exit_reason": reason,
            }
        )
        return net

    for i in range(n):
        sig = evaluate_at(pre, i, pos, p)
        close = float(pre.close[i])

        if pos.side == 0:
            if sig.action in (Action.ENTER_LONG, Action.ENTER_SHORT) and i >= cooloff_until:
                long = sig.action == Action.ENTER_LONG
                fill = close * (1 + slip) if long else close * (1 - slip)
                stop_dist = abs(fill - sig.stop_price)
                if stop_dist <= 0:
                    pass  # degenerate ATR; skip this entry
                else:
                    risk_budget = equity * risk_per_trade_pct
                    qty = risk_budget / stop_dist
                    notional = qty * fill
                    cap = equity * max_leverage
                    if notional > cap:
                        qty = cap / fill
                        notional = qty * fill
                    entry_equity = equity
                    equity -= notional * fee  # entry fee
                    pos = open_from_signal(sig, pre, i, fill)
                    pos.qty = qty
                    pos.entry_equity = entry_equity
        elif sig.action == Action.EXIT:
            long = pos.side == 1
            raw = pos.stop_price if sig.exit_at_stop else close
            exit_fill = raw * (1 - slip) if long else raw * (1 + slip)
            net = _record_trade(i, exit_fill, sig.reason)
            equity += net
            stopped = "stop hit" in sig.reason
            pos = SwitchPosition.flat()
            if stopped:
                cooloff_until = i + cooloff_bars

        # Mark to market for the equity curve.
        if pos.side == 1:
            unreal = pos.qty * (close - pos.entry_price)
        elif pos.side == -1:
            unreal = pos.qty * (pos.entry_price - close)
        else:
            unreal = 0.0
        rows.append(
            {
                "ts": df.index[i],
                "close": close,
                "equity": equity + unreal,
                "position": pos.side,
                "regime": pre.regime[i].value if isinstance(pre.regime[i], Regime) else str(pre.regime[i]),
            }
        )

    curve = pd.DataFrame(rows)
    final_eq = float(curve["equity"].iloc[-1]) if not curve.empty else initial_equity
    final_bh = initial_equity * (float(pre.close[-1]) / initial_close) if initial_close else initial_equity
    return RegimeBacktestResult(
        equity_curve=curve,
        trades=trades,
        initial_equity=float(initial_equity),
        final_equity=final_eq,
        final_buy_and_hold=final_bh,
        funding_applied=funding is not None and not funding.empty,
    )


def summarize(result: RegimeBacktestResult) -> dict:
    """Compact, honest stats — win rate and trade count alongside APR/
    Sharpe, plus per-leg PnL attribution so we can see if one leg is
    carrying the whole result."""
    if result.equity_curve.empty:
        return {}
    eq = result.equity_curve.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    eq = eq.set_index("ts").sort_index()
    span_days = (eq.index[-1] - eq.index[0]).total_seconds() / 86400 or 1.0

    # Infer bars/year from the median bar spacing for Sharpe annualization.
    deltas = eq.index.to_series().diff().dropna().dt.total_seconds()
    bar_seconds = float(deltas.median()) if not deltas.empty else 3600.0
    bars_per_year = (365.25 * 86400) / bar_seconds if bar_seconds > 0 else 8760.0

    ret = eq["equity"].pct_change().dropna()
    if ret.std(ddof=0) > 0:
        sharpe = float(ret.mean() / ret.std(ddof=0) * np.sqrt(bars_per_year))
    else:
        sharpe = 0.0

    def _apr(start: float, end: float) -> float:
        if start <= 0:
            return 0.0
        r = end / start - 1
        return (1 + r) ** (365.0 / span_days) - 1 if r > -1 else -1.0

    def _max_dd(s: pd.Series) -> float:
        peaks = s.cummax()
        return float((s / peaks - 1).min())

    trades = result.trades
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    exposure = float((eq["position"] != 0).mean())

    by_leg: dict[str, float] = {}
    for t in trades:
        by_leg[t["leg"]] = by_leg.get(t["leg"], 0.0) + t["net_pnl"]

    return {
        "span_days": int(span_days),
        "bars_per_year": int(bars_per_year),
        "n_trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "avg_win_pct": float(np.mean([t["return_pct"] for t in wins])) if wins else 0.0,
        "avg_loss_pct": float(np.mean([t["return_pct"] for t in losses])) if losses else 0.0,
        "expectancy_pct": float(np.mean([t["return_pct"] for t in trades])) if trades else 0.0,
        "max_consecutive_losses": _max_consecutive_losses(trades),
        "exposure_pct": exposure,
        "strategy_apr": _apr(result.initial_equity, result.final_equity),
        "buy_and_hold_apr": _apr(result.initial_equity, result.final_buy_and_hold),
        "sharpe": sharpe,
        "max_drawdown": _max_dd(eq["equity"]),
        "final_equity": result.final_equity,
        "pnl_by_leg": by_leg,
        "funding_applied": result.funding_applied,
    }


def _max_consecutive_losses(trades: list[dict]) -> int:
    run = best = 0
    for t in trades:
        if t["net_pnl"] <= 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best
