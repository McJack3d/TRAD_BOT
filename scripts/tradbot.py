"""Terminal app for the BTC trend bot.

A daily-eval trend strategy doesn't need a live-updating dashboard —
one-shot commands are clearer and easier to script.

Usage:
    python -m scripts.tradbot status     # current state
    python -m scripts.tradbot start      # enable trading
    python -m scripts.tradbot stop       # disable trading
    python -m scripts.tradbot evaluate   # fetch + evaluate + trade
    python -m scripts.tradbot flatten    # sell all to USDT/quote
    python -m scripts.tradbot trades     # recent trade log
    python -m scripts.tradbot equity     # equity curve

Paper mode (default): uses Binance public prices, fake balances.
Live mode: set SIMPLE_BOT_LIVE=true + BINANCE_API_KEY/SECRET in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import warnings
from decimal import Decimal
from pathlib import Path

# Silence aiohttp's "Unclosed client session" warning, which fires when
# ccxt fails to reach Binance — the session is created during the failed
# request and can't be cleaned up. Cosmetic only.
warnings.filterwarnings("ignore", message="Unclosed client session")
warnings.filterwarnings("ignore", message="Unclosed connector")
logging.getLogger("asyncio").setLevel(logging.ERROR)

# Make `src.*` importable regardless of where the user runs this from.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.simple_bot import SimpleBot
from src.state.db import Database
from src.state.models import StateSnapshot
from src.strategy.sma_trend import TrendState


DB_PATH = os.environ.get("SIMPLE_BOT_DB", "data/simple_bot.db")
LIVE = os.environ.get("SIMPLE_BOT_LIVE", "false").lower() == "true"
STARTING_USDT = Decimal(os.environ.get("SIMPLE_BOT_STARTING_USDT", "1000"))
SMA_WINDOW = int(os.environ.get("SIMPLE_BOT_SMA_WINDOW", "200"))
ENTRY_BUFFER = float(os.environ.get("SIMPLE_BOT_ENTRY_BUFFER", "0.01"))
EXIT_BUFFER = float(os.environ.get("SIMPLE_BOT_EXIT_BUFFER", "0.01"))
TRAILING_STOP = float(os.environ.get("SIMPLE_BOT_TRAILING_STOP", "0"))
SYMBOL = os.environ.get("SIMPLE_BOT_SYMBOL", "BTC/USDT")


# ---- bot construction -----------------------------------------------


async def make_bot() -> tuple[SimpleBot, "ExchangeAdapter", Database]:  # type: ignore[name-defined]
    db = Database(DB_PATH)
    await db.init(starting_equity=STARTING_USDT)

    if LIVE:
        from src.adapters.binance import BinanceAdapter

        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            raise SystemExit(
                "LIVE mode set but BINANCE_API_KEY / BINANCE_API_SECRET missing. "
                "Check ~/TRAD_BOT/.env"
            )
        testnet = os.environ.get("BINANCE_TESTNET", "false").lower() == "true"
        ex = BinanceAdapter(api_key=api_key, api_secret=api_secret, testnet=testnet)
        await ex.connect()
    else:
        from src.adapters.paper_binance import PaperBinanceAdapter

        ex = PaperBinanceAdapter(starting_usdt=STARTING_USDT)
        try:
            await ex.connect()
        except Exception as e:
            print(
                f"⚠ Couldn't reach Binance public API ({e}). Status will still "
                "load but live prices/evaluation won't work until connectivity returns.",
                file=sys.stderr,
            )

    bot = SimpleBot(
        exchange=ex,
        db=db,
        symbol=SYMBOL,
        sma_window=SMA_WINDOW,
        entry_buffer_pct=ENTRY_BUFFER,
        exit_buffer_pct=EXIT_BUFFER,
        trailing_stop_pct=TRAILING_STOP,
    )
    return bot, ex, db


def _mode_banner(console: Console) -> None:
    if LIVE:
        console.print(
            Panel(
                "[bold red]🚨 LIVE MODE — real money on Binance mainnet 🚨[/]",
                border_style="red",
                expand=False,
            )
        )
    else:
        console.print(
            Panel(
                "[bold green]Paper mode[/] — real Binance prices, fake balances. "
                "No capital at risk.",
                border_style="green",
                expand=False,
            )
        )


# ---- subcommands -----------------------------------------------------


async def cmd_status(args, console: Console) -> int:
    bot, ex, db = await make_bot()
    try:
        _mode_banner(console)
        s = await bot.status()
        table = Table(show_header=False, expand=False, border_style="dim")
        table.add_column(style="dim")
        table.add_column(style="bold")
        table.add_row("Symbol", SYMBOL)
        table.add_row("Trading", "[green]ON[/]" if s.enabled else "[yellow]OFF[/]")
        table.add_row(
            "Position",
            "[cyan]IN[/]" if s.current_state == TrendState.IN else "[dim]OUT[/]",
        )
        if s.last_price:
            table.add_row(f"{s.base_asset} price", f"${s.last_price:,.2f}")
        equity = s.usdt_qty + s.btc_qty * s.last_price
        table.add_row(f"Equity ({s.quote_asset})", f"${equity:,.2f}")
        table.add_row("")
        table.add_row(s.base_asset, f"{s.btc_qty:.8f}")
        table.add_row(s.quote_asset, f"{s.usdt_qty:,.4f}")
        if s.last_signal:
            table.add_row("")
            table.add_row(
                "Last signal",
                f"{s.last_signal.state.value.upper()} — {s.last_signal.reason}",
            )
        if s.last_evaluated:
            table.add_row("Last evaluated", s.last_evaluated.isoformat())
        console.print(Panel(table, title="BTC trend bot", expand=False))
    finally:
        await ex.close()
        await db.close()
    return 0


async def cmd_start(args, console: Console) -> int:
    bot, ex, db = await make_bot()
    try:
        await bot.enable()
        console.print("[green]✓[/] Trading enabled. Run `tradbot evaluate` to check the signal now.")
    finally:
        await ex.close()
        await db.close()
    return 0


async def cmd_stop(args, console: Console) -> int:
    bot, ex, db = await make_bot()
    try:
        await bot.disable()
        console.print(
            "[yellow]⏸[/] Trading disabled. Existing position (if any) is held. "
            "Run `tradbot flatten` to sell it."
        )
    finally:
        await ex.close()
        await db.close()
    return 0


async def cmd_evaluate(args, console: Console) -> int:
    bot, ex, db = await make_bot()
    try:
        _mode_banner(console)
        if not await bot.is_enabled():
            console.print(
                "[yellow]⚠[/] Bot is disabled. Run `tradbot start` first, or "
                "pass --force to evaluate read-only."
            )
            if not args.force:
                return 1
            console.print("[dim]Read-only evaluation (no trade will be placed):[/]")
            sig = await bot.evaluate()
        else:
            with console.status("Fetching daily closes and evaluating..."):
                sig = await bot.tick()

        if sig is None:
            console.print("[yellow]No signal generated (bot may be disabled).[/]")
            return 1

        colour = "cyan" if sig.state == TrendState.IN else "dim"
        console.print(
            f"Signal: [{colour}]{sig.state.value.upper()}[/]  ({sig.reason})"
        )
        # Show fresh status after the tick.
        s = await bot.status()
        console.print(
            f"Position: [{colour}]{s.current_state.value.upper()}[/]  "
            f"{s.base_asset}={s.btc_qty}  {s.quote_asset}={s.usdt_qty}"
        )
    finally:
        await ex.close()
        await db.close()
    return 0


async def cmd_flatten(args, console: Console) -> int:
    bot, ex, db = await make_bot()
    try:
        _mode_banner(console)
        if not args.yes:
            s = await bot.status()
            console.print(
                f"About to sell {s.btc_qty} {s.base_asset} → {s.quote_asset}."
            )
            console.print("Pass --yes to confirm.")
            return 1
        with console.status("Selling..."):
            await bot.flatten_now()
        console.print(f"[green]✓[/] Flattened to {SYMBOL.split('/')[1]}.")
    finally:
        await ex.close()
        await db.close()
    return 0


async def cmd_trades(args, console: Console) -> int:
    from sqlalchemy import desc, select

    from src.state.models import Order

    bot, ex, db = await make_bot()
    try:
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(Order).order_by(desc(Order.submitted_at)).limit(args.limit)
                )
            ).scalars().all()
        if not rows:
            console.print("[dim]No trades recorded yet.[/]")
            return 0
        table = Table(title=f"Last {len(rows)} orders", expand=False)
        table.add_column("submitted")
        table.add_column("symbol")
        table.add_column("side")
        table.add_column("qty", justify="right")
        table.add_column("avg price", justify="right")
        table.add_column("status")
        for o in reversed(rows):
            table.add_row(
                o.submitted_at.strftime("%Y-%m-%d %H:%M"),
                o.symbol,
                o.side.value,
                f"{o.filled_qty:.8f}",
                f"${o.avg_fill_price:,.2f}" if o.avg_fill_price else "—",
                o.status.value,
            )
        console.print(table)
    finally:
        await ex.close()
        await db.close()
    return 0


async def cmd_equity(args, console: Console) -> int:
    from sqlalchemy import select

    bot, ex, db = await make_bot()
    try:
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(StateSnapshot).order_by(StateSnapshot.ts.desc()).limit(args.limit)
                )
            ).scalars().all()
        if not rows:
            console.print("[dim]No equity snapshots yet.[/]")
            return 0
        table = Table(title=f"Last {len(rows)} equity snapshots", expand=False)
        table.add_column("ts")
        table.add_column("equity", justify="right")
        table.add_column("daily PnL", justify="right")
        for snap in reversed(rows):
            table.add_row(
                snap.ts.strftime("%Y-%m-%d %H:%M"),
                f"${snap.equity_usdt:,.2f}",
                f"${snap.realized_pnl_daily:+,.2f}" if snap.realized_pnl_daily else "—",
            )
        console.print(table)
    finally:
        await ex.close()
        await db.close()
    return 0


# ---- entry -----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tradbot",
        description="Terminal app for the BTC trend bot.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show current bot state.")
    sub.add_parser("start", help="Enable trading.")
    sub.add_parser("stop", help="Disable trading (keeps existing position).")
    p_eval = sub.add_parser("evaluate", help="Run one signal-evaluation tick.")
    p_eval.add_argument(
        "--force",
        action="store_true",
        help="Evaluate even when trading is disabled (read-only; no orders).",
    )
    p_flat = sub.add_parser("flatten", help=f"Sell all base to quote currency.")
    p_flat.add_argument("--yes", action="store_true", help="Confirm the sale.")
    p_trd = sub.add_parser("trades", help="List recent orders.")
    p_trd.add_argument("--limit", type=int, default=20)
    p_eq = sub.add_parser("equity", help="Show recent equity snapshots.")
    p_eq.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    console = Console()
    handler = {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "evaluate": cmd_evaluate,
        "flatten": cmd_flatten,
        "trades": cmd_trades,
        "equity": cmd_equity,
    }[args.cmd]

    rc = asyncio.run(handler(args, console))
    sys.exit(rc)


if __name__ == "__main__":
    main()
