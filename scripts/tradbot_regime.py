"""Regime-switch backtest commands for the `tradbot` CLI.

Thin wrapper that lets you launch the backtest from the bot-picker menu
in addition to `python -m scripts.backtest_regime_switch`. Same code
runs underneath; this just feeds argparse defaults from menu choices.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _build_args(
    symbols: str,
    timeframes: str,
    months: int,
    sweep: bool,
    no_funding: bool = False,
):
    """Construct the Namespace the real CLI expects."""
    import argparse

    ns = argparse.Namespace(
        symbols=[s.strip() for s in symbols.split(",") if s.strip()],
        timeframes=[t.strip() for t in timeframes.split(",") if t.strip()],
        months=months,
        equity=1000.0,
        fee_bps=4.0,
        slippage_bps=2.0,
        risk_pct=0.01,
        max_leverage=3.0,
        cooloff=6,
        no_funding=no_funding,
        refresh=False,
        sweep=sweep,
    )
    return ns


async def cmd_regime_backtest(args, console: Console) -> int:
    """Single backtest: BTC+ETH × 5m/15m/1h, 6 months, with funding."""
    from scripts.backtest_regime_switch import _run_one, _scorecard

    ns = _build_args(
        symbols="BTC/USDT,ETH/USDT", timeframes="5m,15m,1h", months=6, sweep=False
    )
    rows: list[dict] = []
    for symbol in ns.symbols:
        for tf in ns.timeframes:
            console.print(f"[dim]running {symbol} {tf}…[/]")
            stats = _run_one(ns, console, symbol, tf)
            if stats:
                rows.append(stats)
    if rows:
        _scorecard(rows, console)
    else:
        console.print(
            "[red]No results.[/] Most likely cause: the host can't reach "
            "Binance. Run this on the Lightsail box (Tokyo region), not on "
            "a geo-blocked network."
        )
    return 0 if rows else 1


async def cmd_regime_sweep(args, console: Console) -> int:
    """Coarse parameter sweep — ADX threshold × ATR-stop multiple."""
    from scripts.backtest_regime_switch import _sweep

    ns = _build_args(
        symbols="BTC/USDT,ETH/USDT", timeframes="1h", months=6, sweep=True
    )
    _sweep(ns, console)
    return 0


async def cmd_regime_quick(args, console: Console) -> int:
    """Quick smoke test: BTC 1h, 2 months, no funding model."""
    from scripts.backtest_regime_switch import _run_one, _scorecard

    ns = _build_args(
        symbols="BTC/USDT", timeframes="1h", months=2, sweep=False, no_funding=True
    )
    console.print("[dim]running quick BTC 1h backtest (2 months, no funding)…[/]")
    rows: list[dict] = []
    stats = _run_one(ns, console, "BTC/USDT", "1h")
    if stats:
        rows.append(stats)
        _scorecard(rows, console)
        return 0
    return 1


def register_subparsers(sub) -> None:
    sub.add_parser("regime-backtest", help="Regime-switch · full backtest (BTC+ETH × 5m/15m/1h, 6mo).")
    sub.add_parser("regime-sweep", help="Regime-switch · parameter sweep (ADX × ATR).")
    sub.add_parser("regime-quick", help="Regime-switch · quick smoke test (BTC 1h, 2mo).")


HANDLERS = {
    "regime-backtest": cmd_regime_backtest,
    "regime-sweep": cmd_regime_sweep,
    "regime-quick": cmd_regime_quick,
}


def menu_items():
    import argparse as _ap

    ns = _ap.Namespace()
    return [
        ("1", "Quick smoke test (BTC 1h, 2 months)", cmd_regime_quick, ns),
        ("2", "Full backtest (BTC+ETH × 5m/15m/1h, 6 months)", cmd_regime_backtest, ns),
        ("3", "Parameter sweep (ADX × ATR-stop)", cmd_regime_sweep, ns),
    ]
