"""Backtest the SMA trend-follower on real BTC daily history.

Pulls daily closes from Binance's public klines endpoint (no auth) and
runs the strategy. Prints a comparison table strategy vs. buy-and-hold.

Usage:
    python -m scripts.backtest_trend --years 5 --sma 50 --equity 1000
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

from src.backtest.trend_backtest import backtest_sma_trend, summarize
from src.logging_setup import configure_logging


async def _fetch_btc_daily(years: int, symbol: str = "BTC/USDT") -> pd.Series:
    client = ccxt.binance({"enableRateLimit": True})
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


def _run(years: int, sma: int, equity: float, fee_bps: float, slip_bps: float) -> None:
    configure_logging("WARNING")
    console = Console()
    console.print(f"[bold]Fetching {years} years of daily BTC closes from Binance...[/]")
    closes = asyncio.run(_fetch_btc_daily(years))
    if closes.empty:
        console.print("[red]No data returned.[/]")
        return
    console.print(f"Got {len(closes)} daily closes from {closes.index[0].date()} to {closes.index[-1].date()}")

    result = backtest_sma_trend(
        closes,
        initial_equity=Decimal(str(equity)),
        sma_window=sma,
        fee_bps=Decimal(str(fee_bps)),
        slippage_bps=Decimal(str(slip_bps)),
    )
    stats = summarize(result)

    table = Table(title=f"SMA-{sma} trend follower vs. buy-and-hold")
    table.add_column("metric")
    table.add_column("strategy", justify="right")
    table.add_column("buy & hold", justify="right")
    table.add_row("Initial equity", f"${equity:,.2f}", f"${equity:,.2f}")
    table.add_row(
        "Final equity",
        f"${stats['strategy_final']:,.2f}",
        f"${stats['buy_and_hold_final']:,.2f}",
    )
    table.add_row(
        "Net APR", f"{stats['strategy_apr']:.2%}", f"{stats['buy_and_hold_apr']:.2%}"
    )
    table.add_row(
        "Max drawdown",
        f"{stats['strategy_max_dd']:.2%}",
        f"{stats['buy_and_hold_max_dd']:.2%}",
    )
    table.add_row("Trades", str(stats["n_trades"]), "1")
    table.add_row("Span (days)", str(stats["span_days"]), str(stats["span_days"]))
    console.print(table)

    # Show the equity curve as ASCII sparkline-style if we're in a terminal.
    console.print("\n[dim]First 5 trades:[/]")
    for t in result.trades[:5]:
        console.print(f"  {t['ts'].date()}  {t['side']:4s}  ${t['price']:>10,.2f}")
    if len(result.trades) > 5:
        console.print(f"  ... and {len(result.trades) - 5} more")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest SMA trend follower on BTC")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--sma", type=int, default=50)
    parser.add_argument("--equity", type=float, default=1000)
    parser.add_argument("--fee-bps", type=float, default=4.0)
    parser.add_argument("--slip-bps", type=float, default=2.0)
    args = parser.parse_args()
    _run(args.years, args.sma, args.equity, args.fee_bps, args.slip_bps)


if __name__ == "__main__":
    main()
