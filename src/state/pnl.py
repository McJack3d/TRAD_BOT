"""Build a correct equity/PnL snapshot for the funding-arb daemon.

This is the piece that makes the daily and cumulative loss-stops in
`src/risk/manager.py` actually work. Previously the daemon wrote
snapshots with `realized_pnl_daily = realized_pnl_cumulative = 0`
hardcoded, so the stops compared `0 <= -threshold` and never fired.

`build_state_snapshot` derives the real numbers from the database
(closed-position PnL + funding payments) and the exchange (open-
position unrealized PnL), so the snapshot the risk manager reads is
truthful.

Realized PnL = closed-position round-trip PnL + funding collected.
Unrealized PnL = sum of open perp-leg unrealized (delta-neutral, so
small in normal operation, but it captures basis blow-outs).
Equity = starting_equity + cumulative_realized + unrealized — an
internally consistent curve that doesn't undercount the long spot leg
the way "sum of USDT balances" would.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.adapters.exchange_base import ExchangeAdapter
from src.logging_setup import log
from src.state.db import Database
from src.state.models import StateSnapshot


def start_of_utc_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Normalize a DB-read datetime to tz-aware UTC.

    SQLite (via aiosqlite) returns tz-naive datetimes even for
    `DateTime(timezone=True)` columns, so any arithmetic against
    `datetime.now(UTC)` raises `TypeError: can't subtract offset-naive
    and offset-aware`. Call this on every datetime read back from the
    database before comparing it to an aware `now`.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _safe_unrealized(exchange: ExchangeAdapter) -> Decimal:
    """Sum unrealized PnL across open positions; 0 on any failure."""
    try:
        positions = await exchange.fetch_positions()
    except Exception as e:  # noqa: BLE001 — a feed hiccup must not block snapshots
        log.warning("pnl.unrealized.fetch_failed", error=str(e))
        return Decimal("0")
    return sum((p.unrealized_pnl for p in positions), start=Decimal("0"))


async def _safe_usdt_balances(exchange: ExchangeAdapter) -> tuple[Decimal, Decimal]:
    """Return (spot_usdt, perp_usdt) best-effort; (0, 0) on failure."""
    try:
        balances = await exchange.fetch_balances()
    except Exception as e:  # noqa: BLE001
        log.warning("pnl.balances.fetch_failed", error=str(e))
        return Decimal("0"), Decimal("0")
    spot = balances.get("spot:USDT")
    perp = balances.get("perp:USDT")
    return (
        spot.total if spot else Decimal("0"),
        perp.total if perp else Decimal("0"),
    )


async def compute_realized_pnl(
    db: Database, now: datetime
) -> tuple[Decimal, Decimal]:
    """Return (daily_realized, cumulative_realized).

    Daily resets naturally at UTC midnight because it's computed as
    "since start of the current UTC day" on every call — no separate
    reset job is needed.
    """
    sod = start_of_utc_day(now)
    daily = (
        await db.realized_position_pnl_since(sod)
        + await db.total_funding_since(sod)
    )
    cumulative = (
        await db.total_realized_position_pnl() + await db.total_funding()
    )
    return daily, cumulative


async def build_state_snapshot(
    db: Database,
    exchange: ExchangeAdapter,
    starting_equity: Decimal,
    now: datetime | None = None,
) -> StateSnapshot:
    now = now or datetime.now(UTC)
    daily_realized, cumulative_realized = await compute_realized_pnl(db, now)
    unrealized = await _safe_unrealized(exchange)
    spot_usdt, perp_usdt = await _safe_usdt_balances(exchange)

    equity = starting_equity + cumulative_realized + unrealized

    return StateSnapshot(
        ts=now,
        equity_usdt=equity,
        spot_balance_usdt=spot_usdt,
        perp_balance_usdt=perp_usdt,
        unrealized_pnl=unrealized,
        realized_pnl_daily=daily_realized,
        realized_pnl_cumulative=cumulative_realized,
    )
