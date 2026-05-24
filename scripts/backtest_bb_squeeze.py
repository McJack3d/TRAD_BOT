"""Backtest the BB-squeeze + RSI intraday strategy on real history.

Pulls intraday OHLCV from Binance's public klines endpoint (no auth)
and runs the strategy bar-by-bar. Prints summary stats and the first
N trades.

Typical usage:

    # 6 months of BTC/USDT on 5-minute bars:
    python -m scripts.backtest_bb_squeeze --months 6 --timeframe 5m

    # Tune the BBW filter and watch the trade count change:
    python -m scripts.backtest_bb_squeeze --min-bbw-pct 50

The fetcher paginates because Binance returns ≤1000 bars per call;
6 months of 5m bars is ~52k bars (≈52 requests).
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

from src.backtest.bb_squeeze_backtest import backtest_bb_squeeze, summarize
from src.logging_setup import configure_logging
from src.strategy.bb_squeeze import SqueezeParams


_TIMEFRAME_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


async def _fetch_intraday(symbol: str, timeframe: str, months: int) -> pd.Series:
    """Fetch `months` of intraday closes, paginating through Binance."""
    if timeframe not in _TIMEFRAME_MS:
        raise SystemExit(f"unsupported timeframe {timeframe}; choose from {list(_TIMEFRAME_MS)}")
    tf_ms = _TIMEFRAME_MS[timeframe]
    client = ccxt.binance({"enableRateLimit": True, "timeout": 30_000})
    try:
        await client.load_markets()
        since = int((datetime.now(UTC) - timedelta(days=months * 30)).timestamp() * 1000)
        rows: list[list] = []
        cursor = since
        console = Console()
        last_pct = -1
        total_ms = int(datetime.now(UTC).timestamp() * 1000) - since
        while True:
            batch = await client.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            last_ts = batch[-1][0]
            if last_ts <= cursor:
                break
            cursor = last_ts + tf_ms
            pct = int(min(100, max(0, (cursor - since) / total_ms * 100)))
            if pct // 10 > last_pct // 10:
                console.print(f"[dim]  fetched up to {datetime.fromtimestamp(last_ts/1000, UTC).date()}  ({pct}%)[/]")
                last_pct = pct
            if len(batch) < 1000:
                break
            await asyncio.sleep(0.1)
    finally:
        await client.close()

    df = pd.DataFrame(rows, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["ts_ms"]).reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    return pd.Series(df["close"].astype(float).values, index=df["ts"])


def _print_summary(title: str, stats: dict, equity: float) -> None:
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
    table.add_row("Trades", str(stats["n_trades"]), "1")
    table.add_row("Win rate", f"{stats['win_rate']:.1%}", "—")
    table.add_row("Avg win",  f"{stats['avg_win_pct']:.2%}", "—")
    table.add_row("Avg loss", f"{stats['avg_loss_pct']:.2%}", "—")
    table.add_row("Avg bars held", f"{stats['avg_bars_held']:.1f}", "—")
    table.add_row("Span (days)", str(stats["span_days"]), str(stats["span_days"]))
    console.print(table)


def _run(
    symbol: str,
    timeframe: str,
    months: int,
    equity: float,
    fee_bps: float,
    slip_bps: float,
    bb_window: int,
    rsi_max: float,
    min_bbw_pct: float,
    bbw_lookback: int,
    setup_expiry: int,
    show_trades: int,
) -> None:
    configure_logging("WARNING")
    console = Console()

    console.print(
        f"[bold]Fetching {months}mo of {timeframe} {symbol} closes from Binance...[/]"
    )
    closes = asyncio.run(_fetch_intraday(symbol, timeframe, months))
    if closes.empty:
        console.print("[red]No data returned.[/]")
        return
    console.print(
        f"Got {len(closes):,} {timeframe} closes from {closes.index[0]} to {closes.index[-1]}\n"
    )

    params = SqueezeParams(
        bb_window=bb_window,
        rsi_entry_max=rsi_max,
        min_bbw_percentile=min_bbw_pct,
        bbw_lookback=bbw_lookback,
        setup_expiry_bars=setup_expiry,
    )
    result = backtest_bb_squeeze(
        closes,
        initial_equity=Decimal(str(equity)),
        fee_bps=Decimal(str(fee_bps)),
        slippage_bps=Decimal(str(slip_bps)),
        params=params,
    )
    stats = summarize(result)
    if not stats:
        console.print("[yellow]Backtest returned no rows.[/]")
        return

    title = (
        f"BB-squeeze + RSI on {symbol} ({timeframe}, {months}mo) — "
        f"BB{bb_window}/RSI<{rsi_max:.0f}, BBW≥p{min_bbw_pct:.0f}"
    )
    _print_summary(title, stats, equity)

    if result.trades and show_trades > 0:
        console.print(f"\n[dim]First {min(show_trades, len(result.trades))} trades:[/]")
        for t in result.trades[:show_trades]:
            mark = "[green]+[/]" if t["pnl"] > 0 else "[red]-[/]"
            console.print(
                f"  {mark} {t['entry_ts'].date()} "
                f"@${t['entry_price']:.2f} → {t['exit_ts'].date()} "
                f"@${t['exit_price']:.2f}  ({t['return_pct']:+.2%}, "
                f"{t['bars_held']} bars)"
            )
        if len(result.trades) > show_trades:
            console.print(f"  ... and {len(result.trades) - show_trades} more")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest BB-squeeze + RSI intraday strategy")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="5m", help="1m, 3m, 5m, 15m, 1h, 4h")
    parser.add_argument("--months", type=int, default=6, help="Months of history to fetch")
    parser.add_argument("--equity", type=float, default=1000)
    parser.add_argument("--fee-bps", type=float, default=4.0)
    parser.add_argument("--slip-bps", type=float, default=2.0)
    parser.add_argument("--bb-window", type=int, default=20)
    parser.add_argument("--rsi-max", type=float, default=25.0, help="Setup requires RSI < this")
    parser.add_argument(
        "--min-bbw-pct", type=float, default=30.0,
        help="Setup requires BBW >= this percentile of recent BBW. 0 = filter off.",
    )
    parser.add_argument("--bbw-lookback", type=int, default=100)
    parser.add_argument("--setup-expiry", type=int, default=6,
                        help="Bars to wait for trigger before disarming.")
    parser.add_argument("--show-trades", type=int, default=10)
    args = parser.parse_args()

    _run(
        symbol=args.symbol,
        timeframe=args.timeframe,
        months=args.months,
        equity=args.equity,
        fee_bps=args.fee_bps,
        slip_bps=args.slip_bps,
        bb_window=args.bb_window,
        rsi_max=args.rsi_max,
        min_bbw_pct=args.min_bbw_pct,
        bbw_lookback=args.bbw_lookback,
        setup_expiry=args.setup_expiry,
        show_trades=args.show_trades,
    )


if __name__ == "__main__":
    main()
