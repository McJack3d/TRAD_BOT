"""Funding-arb daemon monitor commands for the `tradbot` CLI.

Read-only views over the daemon's SQLite DB (`data/bot.db` by default).
No exchange connection — it just reads the state the daemon persists, so
it's safe to run on your laptop against a copied DB or alongside a live
daemon.

The headline command, `farb-status`, surfaces the daily/cumulative
loss-stops that were just fixed: it shows current drawdown and exactly
how much headroom remains before each stop trips.

Mode/paths come from the same env vars the daemon uses:
  BOT_CONFIG   (default config/paper.yaml)
  BOT_DB_PATH  (default data/bot.db)
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import desc, select

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _paths() -> tuple[str, str]:
    from src.config import Secrets

    secrets = Secrets()
    cfg_path = os.environ.get("BOT_CONFIG", secrets.bot_config)
    db_path = os.environ.get("BOT_DB_PATH", secrets.bot_db_path)
    return cfg_path, db_path


async def _load():
    from src.config import BotConfig
    from src.state.db import Database

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


async def cmd_farb_status(args, console: Console) -> int:
    from src.state.models import Position, PositionStatus, SystemStatusEnum

    try:
        cfg, db, db_path = await _load()
    except FileNotFoundError as e:
        console.print(f"[red]✗[/] {e}")
        return 1
    try:
        status = await db.get_status()
        snap = await db.latest_snapshot()
        async with db.session() as s:
            open_count = len(
                (
                    await s.execute(
                        select(Position).where(Position.status == PositionStatus.OPEN)
                    )
                ).scalars().all()
            )

        # Header banner — green ACTIVE, red HALTED.
        if status.status == SystemStatusEnum.HALTED:
            console.print(
                Panel(
                    f"[bold red]🛑 HALTED[/] — {status.halt_reason or 'no reason recorded'}",
                    border_style="red", expand=False,
                )
            )
        elif status.status == SystemStatusEnum.PAUSED:
            console.print(Panel("[bold yellow]⏸ PAUSED[/]", border_style="yellow", expand=False))
        else:
            console.print(Panel("[bold green]● ACTIVE[/]", border_style="green", expand=False))

        starting = cfg.starting_equity_usdt
        table = Table(show_header=False, expand=False, border_style="dim")
        table.add_column(style="dim")
        table.add_column(style="bold")
        table.add_row("DB", db_path)
        table.add_row("Mode", cfg.mode.value)
        table.add_row("Starting equity", f"${starting:,.2f}")
        table.add_row("Open positions", str(open_count))
        table.add_row("Last reconciliation", _age(status.last_reconciliation_ok))

        if snap is None:
            table.add_row("", "")
            table.add_row("Equity", "[dim]no snapshots yet — daemon hasn't ticked[/]")
            console.print(Panel(table, title="Funding-arb daemon", expand=False))
            return 0

        daily_pnl = snap.realized_pnl_daily + snap.unrealized_pnl
        cum_pnl = snap.realized_pnl_cumulative
        daily_stop = starting * cfg.risk.daily_loss_stop_pct
        cum_stop = starting * cfg.risk.cumulative_loss_stop_pct

        def _pnl_cell(v: Decimal) -> str:
            c = "green" if v > 0 else "red" if v < 0 else "dim"
            return f"[{c}]${v:,.2f}[/]"

        table.add_row("Snapshot age", _age(snap.ts))
        table.add_row("Equity", f"${snap.equity_usdt:,.2f}")
        table.add_row("Unrealized PnL", _pnl_cell(snap.unrealized_pnl))
        table.add_row("", "")
        table.add_row("Daily PnL", _pnl_cell(daily_pnl))
        # Loss consumed toward the stop (only when negative).
        daily_used = float(-daily_pnl) if daily_pnl < 0 else 0.0
        table.add_row(
            f"Daily stop (−${daily_stop:,.0f})",
            _bar(daily_used, float(daily_stop)),
        )
        table.add_row("Cumulative PnL", _pnl_cell(cum_pnl))
        cum_used = float(-cum_pnl) if cum_pnl < 0 else 0.0
        table.add_row(
            f"Cumulative stop (−${cum_stop:,.0f})",
            _bar(cum_used, float(cum_stop)),
        )
        console.print(Panel(table, title="Funding-arb daemon", expand=False))
    finally:
        await db.close()
    return 0


async def cmd_farb_positions(args, console: Console) -> int:
    from src.state.models import Position, PositionStatus

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
        table.add_column("symbol")
        table.add_column("spot qty", justify="right")
        table.add_column("perp qty", justify="right")
        table.add_column("opened")
        table.add_column("funding collected", justify="right")
        for p in rows:
            table.add_row(
                p.symbol,
                f"{p.spot_qty:.6f}",
                f"{p.perp_qty:.6f}",
                p.opened_at.strftime("%Y-%m-%d %H:%M") if p.opened_at else "—",
                f"${p.funding_collected:,.2f}",
            )
        console.print(table)
    finally:
        await db.close()
    return 0


async def cmd_farb_equity(args, console: Console) -> int:
    from src.state.models import StateSnapshot

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


def register_subparsers(sub) -> None:
    sub.add_parser("farb-status", help="Funding-arb daemon · status + loss-stop headroom.")
    sub.add_parser("farb-positions", help="Funding-arb daemon · open positions.")
    p_eq = sub.add_parser("farb-equity", help="Funding-arb daemon · equity history.")
    p_eq.add_argument("--limit", type=int, default=20)


HANDLERS = {
    "farb-status": cmd_farb_status,
    "farb-positions": cmd_farb_positions,
    "farb-equity": cmd_farb_equity,
}


def menu_items():
    import argparse as _ap

    ns = _ap.Namespace(limit=20)
    return [
        ("1", "Status + loss-stop headroom", cmd_farb_status, ns),
        ("2", "Open positions", cmd_farb_positions, ns),
        ("3", "Equity history", cmd_farb_equity, ns),
    ]
