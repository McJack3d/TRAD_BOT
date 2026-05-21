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
# Silence asyncio's noisy "Unclosed client session" ERRORs that fire on
# ccxt cleanup paths when Binance was unreachable — they're cosmetic.
logging.getLogger("asyncio").setLevel(logging.CRITICAL + 1)
logging.basicConfig(level=logging.ERROR)
# Silence structlog. The CLI uses rich for visible output; structlog
# would otherwise dump JSON-ish info lines into the same terminal,
# making the user think something went wrong when it didn't.
try:
    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR),
        processors=[structlog.processors.JSONRenderer()],
    )
except ImportError:
    pass

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


async def make_db_only() -> Database:
    """For commands that don't need an exchange (trades, equity, reset)."""
    db = Database(DB_PATH)
    await db.init(starting_equity=STARTING_USDT)
    return db


async def make_bot() -> tuple[SimpleBot, "ExchangeAdapter", Database]:  # type: ignore[name-defined]
    db = await make_db_only()

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

        _, quote = SYMBOL.split("/", maxsplit=1)
        ex = PaperBinanceAdapter(
            starting_usdt=STARTING_USDT, quote_asset=quote, spot_only=True
        )
        try:
            await ex.connect()
        except Exception as e:
            print(
                f"⚠ Couldn't reach Binance public API ({e}). Status will still "
                "load but live prices/evaluation won't work until connectivity returns.",
                file=sys.stderr,
            )

    from src.notify import best_notifier

    bot = SimpleBot(
        exchange=ex,
        db=db,
        symbol=SYMBOL,
        sma_window=SMA_WINDOW,
        entry_buffer_pct=ENTRY_BUFFER,
        exit_buffer_pct=EXIT_BUFFER,
        trailing_stop_pct=TRAILING_STOP,
        notifier=best_notifier(),
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

    db = await make_db_only()
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
        await db.close()
    return 0


async def cmd_equity(args, console: Console) -> int:
    from sqlalchemy import select

    db = await make_db_only()
    try:
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(StateSnapshot).order_by(StateSnapshot.ts.desc()).limit(args.limit)
                )
            ).scalars().all()
        if not rows:
            console.print("[dim]No equity snapshots yet — run `tradbot evaluate` to record one.[/]")
            return 0
        table = Table(title=f"Last {len(rows)} equity snapshots", expand=False)
        table.add_column("ts")
        table.add_column("equity", justify="right")
        table.add_column("Δ", justify="right")
        prev = None
        for snap in reversed(rows):
            delta = ""
            if prev is not None:
                d = float(snap.equity_usdt - prev)
                if d > 0:
                    delta = f"[green]+${d:,.2f}[/]"
                elif d < 0:
                    delta = f"[red]${d:,.2f}[/]"
                else:
                    delta = "[dim]flat[/]"
            table.add_row(
                snap.ts.strftime("%Y-%m-%d %H:%M"),
                f"${snap.equity_usdt:,.2f}",
                delta,
            )
            prev = snap.equity_usdt
        console.print(table)
    finally:
        await db.close()
    return 0


async def cmd_signal(args, console: Console) -> int:
    """Show what the signal says RIGHT NOW without trading. Read-only."""
    bot, ex, db = await make_bot()
    try:
        _mode_banner(console)
        with console.status("Fetching daily closes and evaluating signal..."):
            try:
                sig = await bot.evaluate()
            except Exception as e:
                console.print(f"[red]✗[/] Couldn't fetch closes: {e}")
                return 1
        colour = "cyan" if sig.state == TrendState.IN else "yellow"
        table = Table(show_header=False, expand=False, border_style="dim")
        table.add_column(style="dim")
        table.add_column(style="bold")
        table.add_row("Symbol", SYMBOL)
        table.add_row("SMA window", str(SMA_WINDOW))
        table.add_row("Entry buffer", f"{ENTRY_BUFFER:.2%}")
        table.add_row("Latest close", f"${sig.close:,.2f}")
        table.add_row(f"SMA-{SMA_WINDOW}", f"${sig.sma:,.2f}" if sig.sma > 0 else "—")
        table.add_row("Signal", f"[{colour}]{sig.state.value.upper()}[/]")
        table.add_row("Reason", sig.reason)
        console.print(Panel(table, title="Current signal", expand=False))
        console.print(
            "[dim]This is read-only. Use `tradbot evaluate` to act on the signal.[/]"
        )
    finally:
        await ex.close()
        await db.close()
    return 0


async def cmd_config(args, console: Console) -> int:
    """Print the resolved config and where it comes from."""
    table = Table(title="trad-bot config", expand=False)
    table.add_column("setting")
    table.add_column("value")
    table.add_column("source")
    env_or_default = lambda name, default: ("env" if name in os.environ else "default")
    table.add_row("Symbol", SYMBOL, env_or_default("SIMPLE_BOT_SYMBOL", "BTC/USDT"))
    table.add_row("Mode", "LIVE" if LIVE else "PAPER", env_or_default("SIMPLE_BOT_LIVE", "false"))
    table.add_row("Starting balance", f"${STARTING_USDT}", env_or_default("SIMPLE_BOT_STARTING_USDT", "1000"))
    table.add_row("SMA window", str(SMA_WINDOW), env_or_default("SIMPLE_BOT_SMA_WINDOW", "200"))
    table.add_row("Entry buffer", f"{ENTRY_BUFFER:.2%}", env_or_default("SIMPLE_BOT_ENTRY_BUFFER", "0.01"))
    table.add_row("Exit buffer", f"{EXIT_BUFFER:.2%}", env_or_default("SIMPLE_BOT_EXIT_BUFFER", "0.01"))
    stop_label = f"{TRAILING_STOP:.0%}" if TRAILING_STOP > 0 else "off"
    table.add_row("Trailing stop", stop_label, env_or_default("SIMPLE_BOT_TRAILING_STOP", "0"))
    table.add_row("DB path", DB_PATH, env_or_default("SIMPLE_BOT_DB", "data/simple_bot.db"))
    console.print(table)

    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        console.print(f"\n[dim].env file: {env_path}[/]")
    else:
        console.print(
            f"\n[yellow]No .env file at {env_path} — defaults are in effect.[/]"
        )
    return 0


async def cmd_reset(args, console: Console) -> int:
    """Delete the paper-mode database so you can start fresh."""
    if LIVE and not args.force:
        console.print(
            "[red]✗[/] Refusing to reset in LIVE mode. The DB contains real trade "
            "history. Pass --force if you really mean it (your trades stay on Binance)."
        )
        return 1
    db_path = Path(DB_PATH)
    if not db_path.exists():
        console.print(f"[dim]No DB at {db_path} — already reset.[/]")
        return 0
    if not args.yes:
        console.print(f"About to delete {db_path}. Pass --yes to confirm.")
        return 1
    db_path.unlink()
    console.print(f"[green]✓[/] Removed {db_path}. Next command will create a fresh DB.")
    return 0


async def cmd_watch(args, console: Console) -> int:
    """Refresh status every N seconds until Ctrl+C."""
    from rich.live import Live

    while True:
        try:
            bot, ex, db = await make_bot()
            try:
                s = await bot.status()
                table = Table(show_header=False, expand=False, border_style="dim")
                table.add_column(style="dim")
                table.add_column(style="bold")
                table.add_row("Symbol", SYMBOL)
                table.add_row("Mode", "[red]LIVE[/]" if LIVE else "[green]PAPER[/]")
                table.add_row("Trading", "[green]ON[/]" if s.enabled else "[yellow]OFF[/]")
                table.add_row(
                    "Position",
                    f"[cyan]IN[/]" if s.current_state == TrendState.IN else "[dim]OUT[/]",
                )
                if s.last_price:
                    table.add_row(f"{s.base_asset} price", f"${s.last_price:,.2f}")
                eq = s.usdt_qty + s.btc_qty * s.last_price
                table.add_row(f"Equity ({s.quote_asset})", f"${eq:,.2f}")
                table.add_row(s.base_asset, f"{s.btc_qty:.8f}")
                table.add_row(s.quote_asset, f"{s.usdt_qty:,.4f}")
                from datetime import datetime, timezone

                table.add_row("Last refresh", datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
                console.clear()
                console.print(Panel(table, title=f"BTC trend bot · refresh every {args.interval}s · Ctrl+C to quit", expand=False))
            finally:
                await ex.close()
                await db.close()
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped watching.[/]")
            return 0
        except Exception as e:
            console.print(f"[red]Refresh error:[/] {e}")
        try:
            await asyncio.sleep(args.interval)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped watching.[/]")
            return 0


async def cmd_install_cron(args, console: Console) -> int:
    from src.scheduler import install

    try:
        p = install(_PROJECT_ROOT, hour_utc=args.hour, minute_utc=args.minute)
    except SystemExit as e:
        console.print(f"[red]✗[/] {e}")
        return 1
    console.print(
        f"[green]✓[/] Installed launchd agent at {p.plist}\n"
        f"  Runs daily at {args.hour:02d}:{args.minute:02d} UTC\n"
        f"  Logs: {p.stdout_log}\n"
        f"  Errors: {p.stderr_log}\n"
        f"\nVerify with: launchctl list | grep tradbot"
    )
    return 0


async def cmd_uninstall_cron(args, console: Console) -> int:
    from src.scheduler import uninstall

    removed = uninstall(_PROJECT_ROOT)
    if removed is None:
        console.print("[dim]No launchd agent installed.[/]")
        return 0
    console.print(f"[green]✓[/] Removed {removed}")
    return 0


async def cmd_cron_status(args, console: Console) -> int:
    from src.scheduler import status

    s = status(_PROJECT_ROOT)
    if not s.get("installed"):
        console.print(
            "[dim]No launchd agent installed.[/] Run `tradbot install-cron` to add one."
        )
        return 0
    table = Table(title="launchd auto-eval status", expand=False)
    table.add_column("field")
    table.add_column("value")
    for k, v in s.items():
        if k == "installed":
            v = "[green]yes[/]" if v else "[red]no[/]"
        elif k == "loaded":
            v = "[green]yes[/]" if v else "[red]no[/]"
        table.add_row(k, str(v))
    console.print(table)
    return 0


async def cmd_backtest(args, console: Console) -> int:
    """Run the 5-year backtest and print the comparison table."""
    from src.backtest.trend_backtest import backtest_sma_trend
    from src.backtest.trend_metrics import compute_full_metrics
    from scripts.backtest_trend import _fetch_daily

    console.print(f"[bold]Fetching {args.years}y of daily {SYMBOL} closes...[/]")
    try:
        closes = await _fetch_daily(SYMBOL, args.years)
    except Exception as e:
        console.print(f"[red]✗[/] Couldn't fetch history: {e}")
        return 1
    if closes.empty:
        console.print("[red]✗[/] No data returned.")
        return 1
    console.print(
        f"Got {len(closes)} daily closes from {closes.index[0].date()} to {closes.index[-1].date()}\n"
    )
    result = backtest_sma_trend(
        closes,
        initial_equity=Decimal(str(args.equity)),
        sma_window=SMA_WINDOW,
        entry_buffer_pct=ENTRY_BUFFER,
        exit_buffer_pct=EXIT_BUFFER,
        trailing_stop_pct=TRAILING_STOP,
    )
    m = compute_full_metrics(result)
    table = Table(title=f"{args.years}y backtest of current config", expand=False)
    table.add_column("metric")
    table.add_column("strategy", justify="right")
    table.add_column("buy & hold", justify="right")
    table.add_row("Initial", f"${m.initial_equity:,.2f}", f"${m.initial_equity:,.2f}")
    table.add_row("Final", f"${m.final_equity:,.2f}", "")
    table.add_row("Net APR", f"{m.net_apr:.2%}", f"{m.buy_and_hold_apr:.2%}")
    table.add_row("Sharpe", f"{m.sharpe:.2f}", f"{m.buy_and_hold_sharpe:.2f}")
    table.add_row("Max DD", f"{m.max_drawdown:.2%}", f"{m.buy_and_hold_max_dd:.2%}")
    table.add_row("Trades", str(m.n_trades), "1")
    console.print(table)
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
    sub.add_parser("signal", help="Show what the signal says NOW (read-only).")
    p_flat = sub.add_parser("flatten", help="Sell all base to quote currency.")
    p_flat.add_argument("--yes", action="store_true", help="Confirm the sale.")
    p_trd = sub.add_parser("trades", help="List recent orders.")
    p_trd.add_argument("--limit", type=int, default=20)
    p_eq = sub.add_parser("equity", help="Show recent equity snapshots.")
    p_eq.add_argument("--limit", type=int, default=20)
    sub.add_parser("config", help="Show resolved config + .env location.")
    p_reset = sub.add_parser("reset", help="Delete the paper-mode DB.")
    p_reset.add_argument("--yes", action="store_true", help="Confirm.")
    p_reset.add_argument(
        "--force",
        action="store_true",
        help="Reset even in LIVE mode (loses trade history; Binance is untouched).",
    )
    p_watch = sub.add_parser("watch", help="Refresh status every N seconds.")
    p_watch.add_argument("--interval", type=int, default=30)
    p_bt = sub.add_parser("backtest", help="Run a backtest with the current config.")
    p_bt.add_argument("--years", type=int, default=5)
    p_bt.add_argument("--equity", type=float, default=1000)

    p_ic = sub.add_parser(
        "install-cron",
        help="Install a launchd agent that runs `tradbot evaluate` daily (macOS).",
    )
    p_ic.add_argument(
        "--hour", type=int, default=0,
        help="Hour UTC to run (0-23). Default 0 (just after midnight UTC).",
    )
    p_ic.add_argument("--minute", type=int, default=5)
    sub.add_parser("uninstall-cron", help="Remove the launchd agent.")
    sub.add_parser("cron-status", help="Show launchd agent install state.")

    args = parser.parse_args()
    console = Console()
    handler = {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "evaluate": cmd_evaluate,
        "signal": cmd_signal,
        "flatten": cmd_flatten,
        "trades": cmd_trades,
        "equity": cmd_equity,
        "config": cmd_config,
        "reset": cmd_reset,
        "watch": cmd_watch,
        "backtest": cmd_backtest,
        "install-cron": cmd_install_cron,
        "uninstall-cron": cmd_uninstall_cron,
        "cron-status": cmd_cron_status,
    }[args.cmd]

    rc = asyncio.run(handler(args, console))
    sys.exit(rc)


if __name__ == "__main__":
    main()
