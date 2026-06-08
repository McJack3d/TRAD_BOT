"""Regime-switch backtest and live operations commands for the `tradbot` CLI."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import desc, select, update

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.adapters.exchange_base import ExchangeAdapter, Side
from src.state.db import Database
from src.state.models import (
    Position,
    PositionStatus,
    StateSnapshot,
    SystemStatus,
    SystemStatusEnum,
)
from src.state.pnl import compute_realized_pnl
from src.strategy.regime_live import RegimeLiveBot


def _paths() -> tuple[str, str]:
    from src.config import Secrets

    secrets = Secrets()
    cfg_path = os.environ.get("BOT_CONFIG", "config/regime_switch.yaml")
    db_path = os.environ.get("BOT_DB_PATH", secrets.bot_db_path)
    return cfg_path, db_path


async def _load():
    from src.config import BotConfig

    cfg_path, db_path = _paths()
    if not Path(cfg_path).exists():
        raise FileNotFoundError(f"config not found: {cfg_path}")
    cfg = BotConfig.from_yaml(cfg_path)
    db = Database(db_path)
    await db.init(starting_equity=cfg.starting_equity_usdt)
    return cfg, db, db_path


def _age(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    from src.state.pnl import ensure_utc

    ts = ensure_utc(ts)
    secs = (datetime.now(UTC) - ts).total_seconds()
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    return f"{secs / 3600:.1f}h ago"


def _bar(used: float, total: float, width: int = 20) -> str:
    """A little text gauge: how much of the loss budget is consumed."""
    if total <= 0:
        return "—"
    frac = max(0.0, min(1.0, used / total))
    filled = int(round(frac * width))
    colour = "green" if frac < 0.5 else "yellow" if frac < 0.85 else "red"
    return f"[{colour}]{'█' * filled}{'░' * (width - filled)}[/] {frac:.0%}"


def parse_meta(halt_reason: str | None) -> dict[str, str]:
    if not halt_reason:
        return {}
    out = {}
    for chunk in halt_reason.split("|"):
        if ":" in chunk:
            k, v = chunk.split(":", 1)
            out[k] = v
    return out


def count_consecutive_losses(closed_positions: list[Position]) -> int:
    from src.state.pnl import ensure_utc

    sorted_positions = sorted(
        [p for p in closed_positions if p.closed_at is not None],
        key=lambda p: ensure_utc(p.closed_at),
    )
    count = 0
    for pos in reversed(sorted_positions):
        if pos.realized_pnl < 0:
            count += 1
        else:
            break
    return count


async def _make_live_bot() -> tuple[RegimeLiveBot, ExchangeAdapter, Database]:
    from src.config import Secrets

    cfg, db, _ = await _load()
    secrets = Secrets()

    from src.config import Mode
    is_live = cfg.mode == Mode.LIVE

    if is_live:
        from src.adapters.binance import BinanceAdapter

        api_key = secrets.binance_api_key
        api_secret = secrets.binance_api_secret
        if not api_key or not api_secret:
            raise SystemExit(
                "LIVE mode set in config but BINANCE_API_KEY / BINANCE_API_SECRET missing in .env."
            )
        ex = BinanceAdapter(
            api_key=api_key,
            api_secret=api_secret,
            testnet=secrets.binance_testnet,
        )
        await ex.connect()
    else:
        from src.adapters.paper_binance import PaperBinanceAdapter

        ex = PaperBinanceAdapter(
            starting_usdt=cfg.starting_equity_usdt,
            quote_asset="USDT",
            spot_only=False,
        )
        await ex.connect()

    bot = RegimeLiveBot(
        exchange=ex,
        db=db,
        symbols=[s.perp for s in cfg.symbols],
        config_path=os.environ.get("BOT_CONFIG", "config/regime_switch.yaml"),
    )
    return bot, ex, db


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


# ---- Live operations subcommands ------------------------------------

async def cmd_regime_status(args, console: Console) -> int:
    """Read from config and DB to display a summary of status, positions, realized PnL, headroom, etc."""
    try:
        cfg, db, db_path = await _load()
    except FileNotFoundError as e:
        console.print(f"[red]✗[/] {e}")
        return 1

    if not Path(db_path).exists():
        console.print(f"[yellow]⚠[/] Database not found: {db_path}")
        return 1

    from src.adapters.binance import BinanceAdapter
    from src.config import Secrets

    secrets = Secrets()
    exchange = BinanceAdapter(
        api_key=secrets.binance_api_key,
        api_secret=secrets.binance_api_secret,
        testnet=secrets.binance_testnet,
    )

    try:
        await exchange.connect()
        status = await db.get_status()
        snap = await db.latest_snapshot()
        open_positions = await db.open_positions()

        # Header banner
        meta = parse_meta(status.halt_reason)
        enabled = meta.get("enabled", "false") == "true"

        if status.status == SystemStatusEnum.HALTED:
            console.print(
                Panel(
                    f"[bold red]🛑 HALTED[/] — {status.halt_reason or 'no reason recorded'}",
                    border_style="red",
                    expand=False,
                )
            )
        elif status.status == SystemStatusEnum.PAUSED:
            console.print(
                Panel("[bold yellow]⏸ PAUSED[/]", border_style="yellow", expand=False)
            )
        else:
            if enabled:
                console.print(
                    Panel("[bold green]● ACTIVE[/]", border_style="green", expand=False)
                )
            else:
                console.print(
                    Panel(
                        "[bold yellow]⏸ DISABLED[/]",
                        border_style="yellow",
                        expand=False,
                    )
                )

        starting = status.starting_equity or cfg.starting_equity_usdt

        # Fetch live prices to compute unrealized PnL
        ticker_prices = {}
        try:
            for p in open_positions:
                ticker = await exchange.fetch_ticker(p.symbol, "perp")
                ticker_prices[p.symbol] = ticker.last
        except Exception as e:
            console.print(
                f"[dim]Note: Could not fetch live prices from exchange ({e}). Using snapshot/0 for unrealized PnL.[/]"
            )

        # Print open positions
        if open_positions:
            table_pos = Table(title="Open Positions", expand=False)
            table_pos.add_column("Symbol")
            table_pos.add_column("Contracts/Size", justify="right")
            table_pos.add_column("Side")
            table_pos.add_column("Entry Price", justify="right")
            table_pos.add_column("Stop Price", justify="right")
            table_pos.add_column("Initial Margin", justify="right")
            table_pos.add_column("Unrealized PnL", justify="right")

            for p in open_positions:
                side = "Long" if p.perp_qty > 0 else "Short"
                size = abs(p.perp_qty)
                stop_val = meta.get(f"{p.symbol}_stop_price")
                stop_str = f"${float(stop_val):,.2f}" if stop_val else "—"

                if p.symbol in ticker_prices:
                    u_pnl_val = (ticker_prices[p.symbol] - p.perp_entry_price) * p.perp_qty
                    u_pnl_str = f"${u_pnl_val:,.2f}"
                    if u_pnl_val > 0:
                        u_pnl_str = f"[green]{u_pnl_str}[/]"
                    elif u_pnl_val < 0:
                        u_pnl_str = f"[red]{u_pnl_str}[/]"
                else:
                    u_pnl_str = "[dim]$0.00[/]"

                table_pos.add_row(
                    p.symbol,
                    f"{size:.6f}",
                    side,
                    f"${p.perp_entry_price:,.2f}",
                    stop_str,
                    f"${p.initial_margin:,.2f}",
                    u_pnl_str,
                )
            console.print(table_pos)
        else:
            console.print("[dim]No open perp positions.[/]")

        # PnL / Headroom calculations
        now_utc = datetime.now(UTC)
        daily_realized, cumulative_realized = await compute_realized_pnl(db, now_utc)

        total_unrealized = Decimal("0")
        for p in open_positions:
            if p.symbol in ticker_prices:
                total_unrealized += (
                    ticker_prices[p.symbol] - p.perp_entry_price
                ) * p.perp_qty
            elif snap:
                total_unrealized = snap.unrealized_pnl

        daily_pnl = daily_realized + total_unrealized
        cum_pnl = cumulative_realized

        daily_stop_limit = starting * cfg.risk.daily_loss_stop_pct
        cum_stop_limit = starting * cfg.risk.cumulative_loss_stop_pct

        daily_used = max(Decimal("0"), -daily_pnl)
        cum_used = max(Decimal("0"), -cum_pnl)

        daily_headroom = daily_stop_limit - daily_used
        cum_headroom = cum_stop_limit - cum_used

        # Closed positions for consecutive losses
        async with db.session() as s:
            closed_positions = (
                await s.execute(
                    select(Position).where(Position.status == PositionStatus.CLOSED)
                )
            ).scalars().all()
        consecutive_losses = count_consecutive_losses(closed_positions)

        def _pnl_cell(v: Decimal) -> str:
            c = "green" if v > 0 else "red" if v < 0 else "dim"
            return f"[{c}]${v:,.2f}[/]"

        summary_table = Table(show_header=False, expand=False, border_style="dim")
        summary_table.add_column(style="dim")
        summary_table.add_column(style="bold")
        summary_table.add_row("DB", db_path)
        summary_table.add_row("Config", cfg_path)
        summary_table.add_row("Starting equity", f"${starting:,.2f}")
        summary_table.add_row(
            "Consecutive losses",
            f"{consecutive_losses} / {cfg.risk.max_consecutive_losses}",
        )
        summary_table.add_row("Snapshot age", _age(snap.ts if snap else None))
        summary_table.add_row("Daily realized PnL", _pnl_cell(daily_realized))
        summary_table.add_row(
            "Daily total PnL (realized+unrealized)", _pnl_cell(daily_pnl)
        )
        summary_table.add_row(
            f"Daily stop (−${daily_stop_limit:,.2f})",
            f"{_bar(float(daily_used), float(daily_stop_limit))}  (Headroom: ${daily_headroom:,.2f})",
        )
        summary_table.add_row("Cumulative realized PnL", _pnl_cell(cumulative_realized))
        summary_table.add_row(
            f"Cumulative stop (−${cum_stop_limit:,.2f})",
            f"{_bar(float(cum_used), float(cum_stop_limit))}  (Headroom: ${cum_headroom:,.2f})",
        )
        console.print(Panel(summary_table, title="Regime-switch perp status", expand=False))
    finally:
        await exchange.close()
        await db.close()
    return 0


async def cmd_regime_positions(args, console: Console) -> int:
    """List all open perp positions stored in DB."""
    try:
        cfg, db, _ = await _load()
    except FileNotFoundError as e:
        console.print(f"[red]✗[/] {e}")
        return 1
    try:
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(Position).where(Position.status == PositionStatus.OPEN)
                )
            ).scalars().all()
        if not rows:
            console.print("[dim]No open positions.[/]")
            return 0
        table = Table(title=f"{len(rows)} open positions", expand=False)
        table.add_column("ID")
        table.add_column("Symbol")
        table.add_column("Perp Qty", justify="right")
        table.add_column("Side")
        table.add_column("Entry Price", justify="right")
        table.add_column("Opened At")
        table.add_column("Initial Margin", justify="right")
        for p in rows:
            side = "Long" if p.perp_qty > 0 else "Short"
            table.add_row(
                str(p.id),
                p.symbol,
                f"{abs(p.perp_qty):.6f}",
                side,
                f"${p.perp_entry_price:,.2f}",
                p.opened_at.strftime("%Y-%m-%d %H:%M") if p.opened_at else "—",
                f"${p.initial_margin:,.2f}",
            )
        console.print(table)
    finally:
        await db.close()
    return 0


async def cmd_regime_equity(args, console: Console) -> int:
    """Show a historical table of recent equity snapshots."""
    try:
        cfg, db, _ = await _load()
    except FileNotFoundError as e:
        console.print(f"[red]✗[/] {e}")
        return 1
    try:
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(StateSnapshot)
                    .order_by(desc(StateSnapshot.ts))
                    .limit(args.limit)
                )
            ).scalars().all()
        if not rows:
            console.print("[dim]No equity snapshots yet.[/]")
            return 0
        chronological = list(reversed(rows))
        table = Table(title=f"Last {len(rows)} snapshots", expand=False)
        table.add_column("ts")
        table.add_column("equity", justify="right")
        table.add_column("daily PnL", justify="right")
        table.add_column("cum PnL", justify="right")
        for snap in chronological:
            dc = "green" if snap.realized_pnl_daily >= 0 else "red"
            cc = "green" if snap.realized_pnl_cumulative >= 0 else "red"
            table.add_row(
                snap.ts.strftime("%Y-%m-%d %H:%M"),
                f"${snap.equity_usdt:,.2f}",
                f"[{dc}]${snap.realized_pnl_daily:,.2f}[/]",
                f"[{cc}]${snap.realized_pnl_cumulative:,.2f}[/]",
            )
        console.print(table)
    finally:
        await db.close()
    return 0


async def cmd_regime_enable(args, console: Console) -> int:
    """Mark the bot as enabled and ACTIVE in DB."""
    try:
        cfg, db, _ = await _load()
    except FileNotFoundError as e:
        console.print(f"[red]✗[/] {e}")
        return 1
    try:
        async with db.session() as s:
            row = (
                await s.execute(select(SystemStatus).where(SystemStatus.id == 1))
            ).scalar_one_or_none()
            raw = row.halt_reason if row else None

        meta = parse_meta(raw)
        meta["enabled"] = "true"
        encoded = "|".join(f"{k}:{v}" for k, v in meta.items())

        async with db.session() as s:
            await s.execute(
                update(SystemStatus)
                .where(SystemStatus.id == 1)
                .values(status=SystemStatusEnum.ACTIVE, halt_reason=encoded)
            )
            await s.commit()
        console.print("[green]✓[/] Regime-switching bot enabled and set to ACTIVE.")
    finally:
        await db.close()
    return 0


async def cmd_regime_disable(args, console: Console) -> int:
    """Mark the bot as disabled in DB."""
    try:
        cfg, db, _ = await _load()
    except FileNotFoundError as e:
        console.print(f"[red]✗[/] {e}")
        return 1
    try:
        async with db.session() as s:
            row = (
                await s.execute(select(SystemStatus).where(SystemStatus.id == 1))
            ).scalar_one_or_none()
            raw = row.halt_reason if row else None

        meta = parse_meta(raw)
        meta["enabled"] = "false"
        encoded = "|".join(f"{k}:{v}" for k, v in meta.items())

        async with db.session() as s:
            await s.execute(
                update(SystemStatus)
                .where(SystemStatus.id == 1)
                .values(halt_reason=encoded)
            )
            await s.commit()
        console.print("[yellow]⏸[/] Regime-switching bot disabled.")
    finally:
        await db.close()
    return 0


async def cmd_regime_evaluate(args, console: Console) -> int:
    """Run one single signal evaluation tick immediately."""
    try:
        bot, ex, db = await _make_live_bot()
    except FileNotFoundError as e:
        console.print(f"[red]✗[/] {e}")
        return 1
    try:
        console.print("[dim]Evaluating regime-switching tick...[/]")
        await bot.tick()
        console.print("[green]✓[/] Regime evaluation tick complete.")
    finally:
        await ex.close()
        await db.close()
    return 0


async def cmd_regime_flatten(args, console: Console) -> int:
    """Force close all open perp positions and mark as closed in DB."""
    try:
        bot, ex, db = await _make_live_bot()
    except FileNotFoundError as e:
        console.print(f"[red]✗[/] {e}")
        return 1
    try:
        open_positions = await db.open_positions()
        if not open_positions:
            console.print("[dim]No open positions to flatten.[/]")
            return 0

        if not getattr(args, "yes", False):
            console.print(
                "[yellow]⚠ WARNING: This will immediately close all open perp positions with market orders.[/]"
            )
            for pos in open_positions:
                side = "Long" if pos.perp_qty > 0 else "Short"
                console.print(
                    f"  - {pos.symbol}: {side} {abs(pos.perp_qty)} @ entry {pos.perp_entry_price}"
                )
            console.print("Re-run with --yes to confirm.")
            return 1

        for pos in open_positions:
            console.print(f"Flattening {pos.symbol}...")
            close_side = "sell" if pos.perp_qty > 0 else "buy"
            fill = await bot._close_perp(pos.symbol, close_side, abs(pos.perp_qty), pos.id)
            if fill is not None:
                realized_pnl = Decimal("0")
                if pos.perp_qty > 0:
                    realized_pnl = (fill.avg_price - pos.perp_entry_price) * abs(
                        pos.perp_qty
                    )
                else:
                    realized_pnl = (pos.perp_entry_price - fill.avg_price) * abs(
                        pos.perp_qty
                    )
                await db.close_position(pos.id, realized_pnl=realized_pnl)
                console.print(
                    f"[green]✓[/] Closed {pos.symbol} at avg price ${fill.avg_price:,.2f}. PnL: ${realized_pnl:+.2f}"
                )
            else:
                console.print(
                    f"[red]✗[/] Failed to flatten {pos.symbol} (order rejected/failed)."
                )
    finally:
        await ex.close()
        await db.close()
    return 0


def register_subparsers(sub) -> None:
    sub.add_parser("regime-backtest", help="Regime-switch · full backtest (BTC+ETH × 5m/15m/1h, 6mo).")
    sub.add_parser("regime-sweep", help="Regime-switch · parameter sweep (ADX × ATR).")
    sub.add_parser("regime-quick", help="Regime-switch · quick smoke test (BTC 1h, 2mo).")
    sub.add_parser("regime-diagnose", help="Regime-switch · why-so-few-trades fire-rate diagnostic.")

    sub.add_parser(
        "regime-status",
        help="Regime-switch · current live/paper status and headroom.",
    )
    sub.add_parser("regime-positions", help="Regime-switch · open perp positions.")
    p_eq = sub.add_parser("regime-equity", help="Regime-switch · equity snapshot history.")
    p_eq.add_argument("--limit", type=int, default=20)
    sub.add_parser("regime-enable", help="Regime-switch · enable trading.")
    sub.add_parser("regime-disable", help="Regime-switch · disable trading.")
    sub.add_parser("regime-evaluate", help="Regime-switch · trigger one tick evaluation.")
    p_flat = sub.add_parser("regime-flatten", help="Regime-switch · close all positions.")
    p_flat.add_argument("--yes", action="store_true", help="Confirm execution.")


HANDLERS = {
    "regime-backtest": cmd_regime_backtest,
    "regime-sweep": cmd_regime_sweep,
    "regime-quick": cmd_regime_quick,
    "regime-diagnose": cmd_regime_diagnose,
    "regime-status": cmd_regime_status,
    "regime-positions": cmd_regime_positions,
    "regime-equity": cmd_regime_equity,
    "regime-enable": cmd_regime_enable,
    "regime-disable": cmd_regime_disable,
    "regime-evaluate": cmd_regime_evaluate,
    "regime-flatten": cmd_regime_flatten,
}


def menu_items():
    import argparse as _ap

    ns = _ap.Namespace()
    ns_limit = _ap.Namespace(limit=20)
    ns_flat = _ap.Namespace(yes=False)

    return [
        ("1", "Quick smoke test (BTC 1h, 2 months)", cmd_regime_quick, ns),
        ("2", "Full backtest (BTC+ETH × 5m/15m/1h, 6 months)", cmd_regime_backtest, ns),
        ("3", "Diagnose fire rate (why so few trades)", cmd_regime_diagnose, ns),
        ("4", "Parameter sweep (ADX × ATR-stop)", cmd_regime_sweep, ns),
        ("5", "Status + loss-stop headroom", cmd_regime_status, ns),
        ("6", "Open perp positions", cmd_regime_positions, ns),
        ("7", "Equity snapshot history", cmd_regime_equity, ns_limit),
        ("8", "Enable trading", cmd_regime_enable, ns),
        ("9", "Disable trading", cmd_regime_disable, ns),
        ("10", "Evaluate now (single tick)", cmd_regime_evaluate, ns),
        ("11", "Flatten all perp positions", cmd_regime_flatten, ns_flat),
    ]
