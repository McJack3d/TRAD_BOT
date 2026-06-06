"""Regime-switch backtest commands for the `tradbot` CLI.

Thin wrapper that lets you launch the backtest from the bot-picker menu
in addition to `python -m scripts.backtest_regime_switch`. Both paths
share the same `run_backtest_from_args` async entry point so they
behave identically — and so that a sync wrapper never accidentally
calls `asyncio.run()` from inside a running event loop, which was the
root cause of the misleading 'geo-blocked' error in the first build.
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
    """Construct the Namespace the real CLI's `run_backtest_from_args`
    expects. Defaults mirror the spec's sizing/cost policy and must not
    drift silently — there's a regression test for that."""
    import argparse

    return argparse.Namespace(
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
        diagnose=False,
        debug=False,
        # Parameter overrides — None means "use the spec default."
        no_trend_leg=False,
        no_range_leg=False,
        adx_trend_min=None,
        adx_range_max=None,
        rv_high_pctile=None,
        rv_low_pctile=None,
        atr_mult=None,
        rsi_os=None,
        rsi_ob=None,
    )


async def cmd_regime_backtest(args, console: Console) -> int:
    """Full backtest: BTC+ETH × 5m/15m/1h, 6 months, with funding."""
    from scripts.backtest_regime_switch import run_backtest_from_args

    ns = _build_args(
        symbols="BTC/USDT,ETH/USDT", timeframes="5m,15m,1h", months=6, sweep=False
    )
    return await run_backtest_from_args(ns, console)


async def cmd_regime_sweep(args, console: Console) -> int:
    """Coarse parameter sweep — ADX threshold × ATR-stop multiple."""
    from scripts.backtest_regime_switch import run_backtest_from_args

    ns = _build_args(
        symbols="BTC/USDT,ETH/USDT", timeframes="1h", months=6, sweep=True
    )
    return await run_backtest_from_args(ns, console)


async def cmd_regime_quick(args, console: Console) -> int:
    """Quick smoke test: BTC 1h, 2 months, no funding model."""
    from scripts.backtest_regime_switch import run_backtest_from_args

    ns = _build_args(
        symbols="BTC/USDT", timeframes="1h", months=2, sweep=False, no_funding=True
    )
    console.print("[dim]running quick BTC 1h backtest (2 months, no funding)…[/]")
    return await run_backtest_from_args(ns, console)


async def cmd_regime_diagnose(args, console: Console) -> int:
    """Diagnose WHY the strategy trades so rarely — regime occupancy +
    per-leg entry-condition breakdown across BTC+ETH × 5m/15m/1h."""
    from scripts.backtest_regime_switch import run_diagnose_from_args

    ns = _build_args(
        symbols="BTC/USDT,ETH/USDT", timeframes="5m,15m,1h", months=6, sweep=False
    )
    return await run_diagnose_from_args(ns, console)


def register_subparsers(sub) -> None:
    sub.add_parser("regime-backtest", help="Regime-switch · full backtest (BTC+ETH × 5m/15m/1h, 6mo).")
    sub.add_parser("regime-sweep", help="Regime-switch · parameter sweep (ADX × ATR).")
    sub.add_parser("regime-quick", help="Regime-switch · quick smoke test (BTC 1h, 2mo).")
    sub.add_parser("regime-diagnose", help="Regime-switch · why-so-few-trades fire-rate diagnostic.")


HANDLERS = {
    "regime-backtest": cmd_regime_backtest,
    "regime-sweep": cmd_regime_sweep,
    "regime-quick": cmd_regime_quick,
    "regime-diagnose": cmd_regime_diagnose,
}


def menu_items():
    import argparse as _ap

    ns = _ap.Namespace()
    return [
        ("1", "Quick smoke test (BTC 1h, 2 months)", cmd_regime_quick, ns),
        ("2", "Full backtest (BTC+ETH × 5m/15m/1h, 6 months)", cmd_regime_backtest, ns),
        ("3", "Diagnose fire rate (why so few trades)", cmd_regime_diagnose, ns),
        ("4", "Parameter sweep (ADX × ATR-stop)", cmd_regime_sweep, ns),
    ]
