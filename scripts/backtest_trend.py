"""Backtest the SMA trend-follower on real daily history.

Pulls daily closes from Binance's public klines endpoint (no auth) and
runs the strategy. Prints a comparison table strategy vs. buy-and-hold.

Single-asset:
    python -m scripts.backtest_trend --years 5 --sma 200 --equity 1000

Multi-asset basket (equal-weight):
    python -m scripts.backtest_trend --symbols BTC/USDT,ETH/USDT,SOL/USDT

With a trend-strength buffer (close > SMA * 1.01 to enter):
    python -m scripts.backtest_trend --entry-buffer 0.01
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import ccxt.async_support as ccxt
import pandas as pd
from rich.console import Console
from rich.table import Table

from src.backtest.trend_backtest import (
    backtest_basket,
    backtest_sma_trend,
    summarize,
)
from src.logging_setup import configure_logging


async def _fetch_daily(symbol: str, years: int) -> pd.Series:
    client = ccxt.binance({"enableRateLimit": True, "timeout": 30_000})
    try:
        await client.load_markets()
        since = int((datetime.now(UTC) - timedelta(days=years * 365)).timestamp() * 1000)
        rows: list[list] = []
        cursor = since
        while True:
            batch = await client.fetch_ohlcv(symbol, "1d", since=cursor, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            last_ts = batch[-1][0]
            if last_ts <= cursor:
                break
            cursor = last_ts + 86_400_000
            if len(batch) < 1000:
                break
            await asyncio.sleep(0.1)
    finally:
        await client.close()

    df = pd.DataFrame(rows, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["ts_ms"]).reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return pd.Series(df["close"].astype(float).values, index=df["ts"])


# Keep the old name for backwards-compat with the Streamlit app.
_fetch_btc_daily = lambda years, symbol="BTC/USDT": _fetch_daily(symbol, years)


async def _fetch_all(symbols: list[str], years: int) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for sym in symbols:
        out[sym] = await _fetch_daily(sym, years)
    return out


def _print_table(title: str, stats: dict, n_trades: int, equity: float) -> None:
    console = Console()
    table = Table(title=title)
    table.add_column("metric")
    table.add_column("strategy", justify="right")
    table.add_column("buy & hold", justify="right")
    table.add_row("Initial equity", f"${equity:,.2f}", f"${equity:,.2f}")
    table.add_row(
        "Final equity",
        f"${stats['strategy_final']:,.2f}",
        f"${stats['buy_and_hold_final']:,.2f}",
    )
    table.add_row("Net APR", f"{stats['strategy_apr']:.2%}", f"{stats['buy_and_hold_apr']:.2%}")
    table.add_row(
        "Max drawdown",
        f"{stats['strategy_max_dd']:.2%}",
        f"{stats['buy_and_hold_max_dd']:.2%}",
    )
    table.add_row("Trades", str(n_trades), "1 per asset")
    table.add_row("Span (days)", str(stats["span_days"]), str(stats["span_days"]))
    console.print(table)


def _run(
    years: int,
    sma: int,
    equity: float,
    fee_bps: float,
    slip_bps: float,
    symbols: list[str],
    entry_buffer: float,
    exit_buffer: float,
) -> None:
    configure_logging("WARNING")
    console = Console()

    if len(symbols) == 1:
        sym = symbols[0]
        console.print(f"[bold]Fetching {years}y of daily {sym} closes from Binance...[/]")
        closes = asyncio.run(_fetch_daily(sym, years))
        if closes.empty:
            console.print("[red]No data returned.[/]")
            return
        console.print(
            f"Got {len(closes)} daily closes from {closes.index[0].date()} to {closes.index[-1].date()}"
        )
        result = backtest_sma_trend(
            closes,
            initial_equity=Decimal(str(equity)),
            sma_window=sma,
            fee_bps=Decimal(str(fee_bps)),
            slippage_bps=Decimal(str(slip_bps)),
            entry_buffer_pct=entry_buffer,
            exit_buffer_pct=exit_buffer,
        )
        stats = summarize(result)
        title = f"SMA-{sma} trend follower on {sym}"
        if entry_buffer or exit_buffer:
            title += f"  (buffers: +{entry_buffer:.2%}/-{exit_buffer:.2%})"
        _print_table(title, stats, len(result.trades), equity)
        console.print("\n[dim]First 5 trades:[/]")
        for t in result.trades[:5]:
            console.print(f"  {t['ts'].date()}  {t['side']:4s}  ${t['price']:>10,.2f}")
        if len(result.trades) > 5:
            console.print(f"  ... and {len(result.trades) - 5} more")
        return

    # Multi-asset basket.
    console.print(f"[bold]Fetching {years}y of daily closes for {', '.join(symbols)}...[/]")
    closes_by_symbol = asyncio.run(_fetch_all(symbols, years))
    for sym, s in closes_by_symbol.items():
        console.print(f"  {sym}: {len(s)} closes from {s.index[0].date()} to {s.index[-1].date()}")

    result = backtest_basket(
        closes_by_symbol,
        initial_equity=Decimal(str(equity)),
        sma_window=sma,
        fee_bps=Decimal(str(fee_bps)),
        slippage_bps=Decimal(str(slip_bps)),
        entry_buffer_pct=entry_buffer,
        exit_buffer_pct=exit_buffer,
    )
    stats = summarize(result)
    title = f"SMA-{sma} basket on {', '.join(symbols)}"
    if entry_buffer or exit_buffer:
        title += f"  (buffers: +{entry_buffer:.2%}/-{exit_buffer:.2%})"
    _print_table(title, stats, len(result.trades), equity)
    by_symbol = {}
    for t in result.trades:
        by_symbol[t["symbol"]] = by_symbol.get(t["symbol"], 0) + 1
    console.print("\n[dim]Trades per symbol:[/]")
    for sym, n in by_symbol.items():
        console.print(f"  {sym}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest SMA trend follower")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--sma", type=int, default=200)
    parser.add_argument("--equity", type=float, default=1000)
    parser.add_argument("--fee-bps", type=float, default=4.0)
    parser.add_argument("--slip-bps", type=float, default=2.0)
    parser.add_argument(
        "--symbols",
        default="BTC/USDT",
        help="Comma-separated symbols. One = single-asset. Multiple = equal-weight basket.",
    )
    parser.add_argument(
        "--entry-buffer",
        type=float,
        default=0.0,
        help="Require close > SMA * (1 + buffer) to enter. e.g. 0.01 = 1%% buffer.",
    )
    parser.add_argument(
        "--exit-buffer",
        type=float,
        default=0.0,
        help="Require close < SMA * (1 - buffer) to exit.",
    )
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    _run(
        years=args.years,
        sma=args.sma,
        equity=args.equity,
        fee_bps=args.fee_bps,
        slip_bps=args.slip_bps,
        symbols=symbols,
        entry_buffer=args.entry_buffer,
        exit_buffer=args.exit_buffer,
    )


if __name__ == "__main__":
    main()
