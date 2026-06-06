"""Backtest the regime-switching long/short perp strategy.

Downloads BTC/ETH perp history from Binance, runs the strategy across
one or more timeframes, and prints an honest scorecard against the
acceptance gates in docs/REGIME_SWITCH_STRATEGY.md.

Run this on a host with Binance access (your Lightsail box in Tokyo) —
not from a geo-blocked network.

Examples:
    # Default sweep: BTC+ETH, 5m/15m/1h, 6 months, with funding
    python -m scripts.backtest_regime_switch

    # One symbol/timeframe, 12 months, no funding model
    python -m scripts.backtest_regime_switch --symbols BTC/USDT --timeframes 1h \
        --months 12 --no-funding

    # Coarse parameter sweep over ADX threshold x ATR stop multiple
    python -m scripts.backtest_regime_switch --sweep
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from rich.console import Console
from rich.table import Table

from src.backtest.regime_switch_backtest import backtest_regime_switch, summarize
from src.strategy.regime_switch import RegimeSwitchParams

# Acceptance gates (docs/REGIME_SWITCH_STRATEGY.md §11).
GATE_SHARPE = 1.0
GATE_MAX_DD = -0.35
GATE_MIN_TRADES = 100


def _load(symbol: str, timeframe: str, months: int, refresh: bool, use_funding: bool):
    from src.data.history import load_funding, load_ohlcv

    df = load_ohlcv(symbol, timeframe, months=months, refresh=refresh)
    funding = None
    if use_funding:
        try:
            funding = load_funding(symbol, months=months, refresh=refresh)
        except Exception:
            funding = None
    return df, funding


def _gate_cell(stats: dict) -> str:
    ok = (
        stats.get("sharpe", 0) >= GATE_SHARPE
        and stats.get("max_drawdown", -1) >= GATE_MAX_DD
        and stats.get("n_trades", 0) >= GATE_MIN_TRADES
    )
    return "[green]PASS[/]" if ok else "[yellow]review[/]"


def _run_one(args, console: Console, symbol: str, timeframe: str) -> dict | None:
    try:
        df, funding = _load(symbol, timeframe, args.months, args.refresh, not args.no_funding)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]✗[/] {symbol} {timeframe}: download failed — {e}")
        return None
    if df.empty or len(df) < 300:
        console.print(f"[yellow]⚠[/] {symbol} {timeframe}: only {len(df)} bars — skipping")
        return None
    res = backtest_regime_switch(
        df,
        params=RegimeSwitchParams(),
        initial_equity=args.equity,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        risk_per_trade_pct=args.risk_pct,
        max_leverage=args.max_leverage,
        cooloff_bars=args.cooloff,
        funding=funding,
    )
    stats = summarize(res)
    stats["symbol"] = symbol
    stats["timeframe"] = timeframe
    return stats


def _scorecard(rows: list[dict], console: Console) -> None:
    table = Table(title="Regime-switch backtest scorecard", expand=False)
    for col in ("symbol", "tf", "trades", "win%", "Sharpe", "max DD", "APR", "vs B&H APR", "expo%", "gate"):
        table.add_column(col, justify="right" if col not in ("symbol", "tf", "gate") else "left")
    for s in rows:
        table.add_row(
            s["symbol"],
            s["timeframe"],
            str(s["n_trades"]),
            f"{s['win_rate']:.0%}",
            f"{s['sharpe']:.2f}",
            f"{s['max_drawdown']:.1%}",
            f"{s['strategy_apr']:.1%}",
            f"{s['buy_and_hold_apr']:.1%}",
            f"{s['exposure_pct']:.0%}",
            _gate_cell(s),
        )
    console.print(table)
    # Per-leg attribution for the best row.
    if rows:
        best = max(rows, key=lambda s: s["sharpe"])
        legs = ", ".join(f"{k or 'n/a'}: ${v:,.0f}" for k, v in best.get("pnl_by_leg", {}).items())
        console.print(
            f"[dim]Best Sharpe: {best['symbol']} {best['timeframe']} — "
            f"PnL by leg → {legs or 'none'}. "
            f"Funding modeled: {best.get('funding_applied')}[/]"
        )
    console.print(
        f"[dim]Gates: Sharpe ≥ {GATE_SHARPE}, max DD ≥ {GATE_MAX_DD:.0%}, "
        f"trades ≥ {GATE_MIN_TRADES}. 'PASS' is necessary but NOT sufficient — "
        f"also confirm profit in both a trending and a ranging sub-period, and "
        f"walk-forward, before paper.[/]"
    )


def _sweep(args, console: Console) -> None:
    grid_adx = [20.0, 25.0, 30.0]
    grid_atr = [1.5, 2.0, 3.0]
    for symbol in args.symbols:
        for timeframe in args.timeframes:
            try:
                df, funding = _load(symbol, timeframe, args.months, args.refresh, not args.no_funding)
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]✗[/] {symbol} {timeframe}: {e}")
                continue
            if df.empty or len(df) < 300:
                console.print(f"[yellow]⚠[/] {symbol} {timeframe}: too few bars")
                continue
            table = Table(title=f"Sweep — {symbol} {timeframe}", expand=False)
            for col in ("adx_min", "atr_mult", "trades", "Sharpe", "max DD", "APR"):
                table.add_column(col, justify="right")
            best = None
            for adx_min in grid_adx:
                for atr_mult in grid_atr:
                    res = backtest_regime_switch(
                        df,
                        params=RegimeSwitchParams(adx_trend_min=adx_min, atr_mult=atr_mult),
                        initial_equity=args.equity,
                        fee_bps=args.fee_bps,
                        slippage_bps=args.slippage_bps,
                        risk_per_trade_pct=args.risk_pct,
                        max_leverage=args.max_leverage,
                        cooloff_bars=args.cooloff,
                        funding=funding,
                    )
                    s = summarize(res)
                    table.add_row(
                        f"{adx_min:.0f}", f"{atr_mult:.1f}", str(s["n_trades"]),
                        f"{s['sharpe']:.2f}", f"{s['max_drawdown']:.1%}", f"{s['strategy_apr']:.1%}",
                    )
                    if best is None or s["sharpe"] > best[1]["sharpe"]:
                        best = ((adx_min, atr_mult), s)
            console.print(table)
            if best:
                console.print(
                    f"[green]best[/] {symbol} {timeframe}: adx_min={best[0][0]}, "
                    f"atr_mult={best[0][1]} → Sharpe {best[1]['sharpe']:.2f}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the regime-switch strategy.")
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT")
    parser.add_argument("--timeframes", default="5m,15m,1h")
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--equity", type=float, default=1000.0)
    parser.add_argument("--fee-bps", type=float, default=4.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--risk-pct", type=float, default=0.01)
    parser.add_argument("--max-leverage", type=float, default=3.0)
    parser.add_argument("--cooloff", type=int, default=6)
    parser.add_argument("--no-funding", action="store_true", help="Skip the funding cost model.")
    parser.add_argument("--refresh", action="store_true", help="Force re-download (ignore cache).")
    parser.add_argument("--sweep", action="store_true", help="Coarse param grid instead of a single run.")
    args = parser.parse_args()
    args.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    args.timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    console = Console()
    if args.sweep:
        _sweep(args, console)
        return

    rows: list[dict] = []
    for symbol in args.symbols:
        for timeframe in args.timeframes:
            console.print(f"[dim]running {symbol} {timeframe}…[/]")
            stats = _run_one(args, console, symbol, timeframe)
            if stats:
                rows.append(stats)
    if rows:
        _scorecard(rows, console)
    else:
        console.print("[red]No results — check network access to Binance.[/]")


if __name__ == "__main__":
    main()
