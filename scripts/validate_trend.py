"""Full validation suite for the trend strategy before going live.

Runs four checks:

1. In-sample backtest with the recommended config. Reports full metrics
   (Sharpe, Sortino, Calmar, Ulcer index) alongside APR and drawdown.
2. Out-of-sample split: tune nothing on the last 30% of data; report
   the strategy on that held-out window. Honest number for forward
   expectations.
3. Walk-forward: rolling 2y train / 6mo test windows, with grid search
   over (sma_window, buffer) on each train window. Reports a table of
   test-window outcomes. Strategy is robust iff most test Sharpes > 0.
4. Per-asset generalization: same recommended config on BTC, ETH, SOL
   individually. The rule shouldn't only work on BTC.

Usage:
    python -m scripts.validate_trend
    python -m scripts.validate_trend --years 5 --sma 200 --buffer 0.01 --trailing 0.15
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from rich.console import Console
from rich.table import Table

from src.backtest.trend_backtest import backtest_sma_trend
from src.backtest.trend_metrics import compute_full_metrics
from src.backtest.trend_walk_forward import oos_split, walk_forward_trend
from src.logging_setup import configure_logging
from scripts.backtest_trend import _fetch_daily


def _print_metrics(console: Console, title: str, m, equity: float) -> None:
    table = Table(title=title)
    table.add_column("metric")
    table.add_column("strategy", justify="right")
    table.add_column("buy & hold", justify="right")
    table.add_row("Initial", f"${equity:,.0f}", f"${equity:,.0f}")
    table.add_row("Final", f"${m.final_equity:,.2f}", "")
    table.add_row("Net APR", f"{m.net_apr:.2%}", f"{m.buy_and_hold_apr:.2%}")
    table.add_row("Sharpe", f"{m.sharpe:.2f}", f"{m.buy_and_hold_sharpe:.2f}")
    table.add_row("Sortino", f"{m.sortino:.2f}", "")
    table.add_row("Calmar", f"{m.calmar:.2f}", "")
    table.add_row("Max DD", f"{m.max_drawdown:.2%}", f"{m.buy_and_hold_max_dd:.2%}")
    table.add_row("Ulcer Index", f"{m.ulcer_index:.2f}", "")
    table.add_row("Trades", str(m.n_trades), "1")
    table.add_row("Win rate", f"{m.win_rate:.1%}", "")
    table.add_row("Span (days)", str(m.span_days), "")
    console.print(table)


def _run(years: int, sma: int, buffer: float, trailing: float, equity: float) -> None:
    configure_logging("WARNING")
    console = Console()
    console.print(f"[bold]Fetching {years}y of daily BTC closes from Binance...[/]")
    closes = asyncio.run(_fetch_daily("BTC/USDT", years))
    if closes.empty:
        console.print("[red]No data returned.[/]")
        return
    console.print(
        f"Got {len(closes)} daily closes from {closes.index[0].date()} to {closes.index[-1].date()}\n"
    )

    common = dict(
        initial_equity=Decimal(str(equity)),
        sma_window=sma,
        entry_buffer_pct=buffer,
        exit_buffer_pct=buffer,
        trailing_stop_pct=trailing,
    )

    # 1. In-sample (full window).
    console.rule("[bold]1. In-sample backtest (full window)[/]")
    r = backtest_sma_trend(closes, **common)
    m = compute_full_metrics(r)
    _print_metrics(console, "BTC SMA-{} buf {:.0%} stop {:.0%} — in-sample".format(sma, buffer, trailing), m, equity)

    # 2. Out-of-sample split. Use the in-sample params on the held-out window.
    console.rule("[bold]2. Out-of-sample evaluation (held-out final 30%)[/]")
    is_closes, oos_closes = oos_split(closes, oos_fraction=0.30)
    console.print(
        f"In-sample window:  {is_closes.index[0].date()} → {is_closes.index[-1].date()}  ({len(is_closes)} bars)"
    )
    console.print(
        f"Out-of-sample:     {oos_closes.index[0].date()} → {oos_closes.index[-1].date()}  ({len(oos_closes)} bars)\n"
    )
    is_r = backtest_sma_trend(is_closes, **common)
    oos_r = backtest_sma_trend(oos_closes, **common)
    _print_metrics(console, "BTC IN-SAMPLE", compute_full_metrics(is_r), equity)
    _print_metrics(console, "BTC OUT-OF-SAMPLE (the honest number)", compute_full_metrics(oos_r), equity)

    # 3. Walk-forward.
    console.rule("[bold]3. Walk-forward validation (rolling 2y train / 6mo test)[/]")
    windows = walk_forward_trend(closes, train_days=730, test_days=180, initial_equity=Decimal(str(equity)), trailing_stop_pct=trailing)
    if not windows:
        console.print("[yellow]Not enough data for walk-forward (need > 2.5y).[/]")
    else:
        wf_table = Table(title="Walk-forward windows (test-window metrics only)")
        wf_table.add_column("test_start")
        wf_table.add_column("test_end")
        wf_table.add_column("sma", justify="right")
        wf_table.add_column("buf", justify="right")
        wf_table.add_column("APR", justify="right")
        wf_table.add_column("Sharpe", justify="right")
        wf_table.add_column("Max DD", justify="right")
        positives = 0
        for w in windows:
            tm = w.test_metrics
            if tm.sharpe > 0:
                positives += 1
            wf_table.add_row(
                w.test_start.date().isoformat(),
                w.test_end.date().isoformat(),
                str(w.best_sma),
                f"{w.best_buffer:.0%}",
                f"{tm.net_apr:.2%}",
                f"{tm.sharpe:.2f}",
                f"{tm.max_drawdown:.2%}",
            )
        console.print(wf_table)
        console.print(
            f"\n[bold]{positives}/{len(windows)}[/] test windows had positive Sharpe."
        )
        if positives / max(1, len(windows)) >= 0.6:
            console.print("[green]✓ Strategy looks robust across regimes.[/]")
        else:
            console.print("[red]✗ Strategy not robust enough — fewer than 60% of test windows positive.[/]")

    # 4. Per-asset.
    console.rule("[bold]4. Per-asset generalization[/]")
    for sym in ("ETH/USDT", "SOL/USDT"):
        try:
            sym_closes = asyncio.run(_fetch_daily(sym, years))
        except Exception as e:
            console.print(f"[yellow]Couldn't fetch {sym}: {e}[/]")
            continue
        if sym_closes.empty:
            console.print(f"[yellow]No data for {sym}.[/]")
            continue
        sr = backtest_sma_trend(sym_closes, **common)
        sm = compute_full_metrics(sr)
        _print_metrics(console, f"{sym} (same params as BTC)", sm, equity)


def main() -> None:
    parser = argparse.ArgumentParser(description="Full validation of the trend strategy")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--sma", type=int, default=200)
    parser.add_argument("--buffer", type=float, default=0.01)
    parser.add_argument("--trailing", type=float, default=0.0)
    parser.add_argument("--equity", type=float, default=1000)
    args = parser.parse_args()
    _run(args.years, args.sma, args.buffer, args.trailing, args.equity)


if __name__ == "__main__":
    main()
