"""Two-sided funding-carry backtest commands for the `tradbot` CLI.

Runs the carry backtester (`src.backtest.carry_backtest`) over real
Binance funding + cross-margin borrow-rate history and prints, per
symbol, the per-leg attribution and the four spec §6 acceptance gates.

This is **Step 5** of the build (`docs/FUNDING_CARRY_2SIDED.md` §7): the
go/no-go on the negative leg. It must run where Binance is reachable —
the Lightsail (Tokyo) box — because the carry data lives behind the same
geo-gated API as the OHLCV loader. From a blocked network it fails fast
with a clear diagnosis rather than a misleading 'check your connection'.

The decision logic is identical to what the live bot will run: the
backtester calls the same pure functions in `src.strategy.funding_carry`,
so a green backtest here is a real statement about the live strategy.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.table import Table

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_SYMBOLS = ("BTC/USDT", "ETH/USDT")  # negative-leg universe (spec §9.3)


def _explain(e: Exception) -> str:
    from scripts.backtest_regime_switch import _explain_download_failure

    return _explain_download_failure(e)


def _render_result(console: Console, res) -> None:
    """Per-leg attribution table for one symbol's carry backtest."""
    from src.backtest.carry_backtest import carry_acceptance_gates, gates_summary

    span = ""
    if not res.equity_curve.empty:
        span = (
            f"  {res.equity_curve.index[0].date()} → "
            f"{res.equity_curve.index[-1].date()}"
        )
    table = Table(
        title=f"Funding carry — {res.symbol}{span}", expand=False, show_lines=False
    )
    for col in ("leg", "episodes", "settlements", "total return", "Sharpe", "max DD"):
        table.add_column(col, justify="left" if col == "leg" else "right")

    for leg, label in ((res.positive, "positive"), (res.negative, "negative")):
        ret_colour = "green" if leg.total_return > 0 else "red"
        table.add_row(
            label,
            str(leg.n_episodes),
            str(leg.settlements_held),
            f"[{ret_colour}]{leg.total_return:+.2%}[/]",
            f"{leg.sharpe:.2f}",
            f"{leg.max_drawdown:.2%}",
        )
    table.add_row(
        "[bold]combined[/]",
        str(len(res.episodes)),
        "—",
        f"{res.total_return:+.2%}",
        f"{res.combined_sharpe:.2f}",
        f"{res.max_drawdown:.2%}",
        end_section=True,
    )
    console.print(table)
    console.print(
        f"  start ${float(res.initial_equity):,.0f} → "
        f"end ${float(res.final_equity):,.0f}"
    )
    console.print(gates_summary(carry_acceptance_gates(res)))


async def cmd_carry_backtest(args, console: Console) -> int:
    """Replay BTC + ETH funding + borrow history, attribute PnL by leg,
    and check the negative-leg acceptance gates. Go/no-go for the build."""
    from src.backtest.carry_backtest import backtest_carry, carry_acceptance_gates
    from src.data.history import load_borrow_rate_async, load_funding_async

    symbols = getattr(args, "symbols", None) or list(_DEFAULT_SYMBOLS)
    months = getattr(args, "months", 48)
    equity = Decimal(str(getattr(args, "equity", 1000.0)))
    fee_bps = Decimal(str(getattr(args, "fee_bps", 4.0)))
    slip_bps = Decimal(str(getattr(args, "slippage_bps", 2.0)))
    refresh = getattr(args, "refresh", False)

    console.print(
        f"[dim]Carry backtest · {', '.join(symbols)} · {months} months · "
        f"fees {fee_bps}bps + slip {slip_bps}bps[/]"
    )
    console.print(
        "[dim]Negative-leg gates (spec §6): net-positive after borrow+fees, "
        "Sharpe>1, ≥20 episodes, combined DD ≤ one-sided + 5pp.[/]\n"
    )

    total_neg_eps = 0
    universe_all_pass = True
    ran_any = False
    for symbol in symbols:
        asset = symbol.split("/")[0]
        console.print(
            f"[dim]Loading {symbol} funding + {asset} borrow history…[/]"
        )
        try:
            funding = await load_funding_async(symbol, months=months, refresh=refresh)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]✗[/] {symbol} funding: {_explain(e)}")
            return 1
        try:
            borrow = await load_borrow_rate_async(asset, months=months, refresh=refresh)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]✗[/] {asset} borrow rate: {_explain(e)}")
            return 1

        if funding.empty:
            console.print(f"[yellow]⚠[/] no funding history for {symbol} — skipping.")
            continue
        if borrow.empty:
            console.print(
                f"[yellow]⚠[/] no borrow history for {asset}; the negative leg "
                "will refuse every entry (no-data ⇒ no-trade). Positive leg still runs."
            )

        res = backtest_carry(
            funding, borrow, symbol,
            initial_equity=equity, fee_bps=fee_bps, slippage_bps=slip_bps,
        )
        _render_result(console, res)
        console.print("")
        total_neg_eps += res.negative.n_episodes
        universe_all_pass = universe_all_pass and all(
            g.passed for g in carry_acceptance_gates(res)
        )
        ran_any = True

    if not ran_any:
        console.print("[red]✗[/] No symbol produced a backtest (no data).")
        return 1

    console.print(
        f"[bold]Universe verdict:[/] {total_neg_eps} negative-leg episodes total · "
        + (
            "[green]all per-symbol gates PASS — proceed to step 6 "
            "(state/risk/execution wiring).[/]"
            if universe_all_pass
            else "[red]gates FAILED — ship positive-only and shelve the negative "
            "leg with a post-mortem (same discipline as the regime build).[/]"
        )
    )
    return 0 if universe_all_pass else 2


def register_subparsers(sub) -> None:
    p = sub.add_parser(
        "carry-backtest",
        help="Two-sided funding carry · backtest + acceptance gates (BTC+ETH).",
    )
    p.add_argument(
        "--symbols", nargs="+", default=list(_DEFAULT_SYMBOLS),
        help="Symbols to test (default: BTC/USDT ETH/USDT).",
    )
    p.add_argument(
        "--months", type=int, default=48,
        help="History depth in months (default 48 — reaches the 2022 bear).",
    )
    p.add_argument("--equity", type=float, default=1000.0)
    p.add_argument("--fee-bps", dest="fee_bps", type=float, default=4.0)
    p.add_argument("--slippage-bps", dest="slippage_bps", type=float, default=2.0)
    p.add_argument(
        "--refresh", action="store_true", help="Bypass the Parquet cache."
    )


HANDLERS = {
    "carry-backtest": cmd_carry_backtest,
}


def menu_items():
    import argparse as _ap

    ns = _ap.Namespace(
        symbols=list(_DEFAULT_SYMBOLS), months=48, equity=1000.0,
        fee_bps=4.0, slippage_bps=2.0, refresh=False,
    )
    return [
        ("1", "Carry backtest + gates (BTC+ETH, 48 months)", cmd_carry_backtest, ns),
    ]
