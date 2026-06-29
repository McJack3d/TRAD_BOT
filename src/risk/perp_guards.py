"""Per-asset and account-level risk guards for the perp trading strategy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from src.risk.checks import CheckResult
from src.state.db import Database
from src.state.models import SystemStatusEnum

logger = logging.getLogger(__name__)


class PerpRiskConfig(BaseModel):
    cooloff_bars: int = Field(6, ge=0)
    per_asset_daily_pct: Decimal = Field(Decimal("0.015"), gt=Decimal("0"))
    max_consecutive_losses: int = Field(4, ge=1)


@dataclass(slots=True)
class PerpRiskParams:
    cooloff_bars: int = 6
    per_asset_daily_pct: Decimal = Decimal("0.015")
    max_consecutive_losses: int = 4


def _get_field(obj: Any, keys: list[str], default: Any = None) -> Any:
    """Helper to extract a field from a dictionary or object dynamically."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
    else:
        for k in keys:
            if hasattr(obj, k):
                return getattr(obj, k)
    return default


def check_asset_cooloff(
    symbol: str,
    closed_trades: list[Any],
    current_bar_index: int,
    cooloff_bars: int = 6,
) -> CheckResult:
    """Determine if an asset is under cool-off based on recent trades.

    If the last position close for the asset was a loss (stopped-out),
    block new entries for `cooloff_bars` on that asset.
    """
    symbol_trades = []
    for t in closed_trades:
        sym = _get_field(t, ["symbol", "asset"])
        if sym == symbol:
            symbol_trades.append(t)

    if not symbol_trades:
        return CheckResult.pass_()

    # Sort symbol trades chronologically
    def sort_key(t: Any) -> tuple[int, Any]:
        # Prefer absolute UTC timestamps for cross-asset correctness
        ts = _get_field(t, ["exit_ts", "closed_at", "exit_time", "timestamp", "ts"])
        if ts is not None:
            return (0, ts)
        bar_idx = _get_field(
            t,
            [
                "exit_bar_index",
                "closed_bar_index",
                "exit_index",
                "closed_at_index",
                "exit_bar",
                "bar_index",
            ],
        )
        if bar_idx is not None:
            return (1, int(bar_idx))
        return (2, 0)

    symbol_trades = sorted(symbol_trades, key=sort_key)
    last_trade = symbol_trades[-1]

    pnl = _get_field(last_trade, ["net_pnl", "realized_pnl", "pnl"])
    if pnl is None:
        return CheckResult.pass_()

    pnl_dec = Decimal(str(pnl))
    if pnl_dec >= 0:
        return CheckResult.pass_()

    # It's a loss. Check elapsed bars.
    exit_bar = _get_field(
        last_trade,
        [
            "exit_bar_index",
            "closed_bar_index",
            "exit_index",
            "closed_at_index",
            "exit_bar",
            "bar_index",
        ],
    )
    if exit_bar is None:
        return CheckResult.pass_()

    elapsed = current_bar_index - int(exit_bar)
    if elapsed < cooloff_bars:
        return CheckResult.fail(
            f"Asset {symbol} is in cool-off: {elapsed} bars elapsed since last loss at bar {exit_bar} (cool-off: {cooloff_bars} bars)"
        )

    return CheckResult.pass_()


def check_asset_daily_stop(
    symbol: str,
    asset_realized_pnl: Decimal,
    asset_unrealized_pnl: Decimal,
    account_equity: Decimal,
    per_asset_daily_pct: Decimal = Decimal("0.015"),
) -> CheckResult:
    """Determine if an asset's daily PnL (realized + unrealized) is <= -per_asset_daily_pct of the account equity.

    If so, pause trading for that asset for the rest of the UTC day.
    """
    daily_pnl = Decimal(str(asset_realized_pnl)) + Decimal(str(asset_unrealized_pnl))
    stop_limit = Decimal(str(per_asset_daily_pct)) * Decimal(str(account_equity))
    if daily_pnl <= -stop_limit:
        return CheckResult.fail(
            f"Asset {symbol} daily stop hit: daily PnL {daily_pnl} <= limit -{stop_limit} "
            f"({per_asset_daily_pct * 100}% of equity {account_equity})"
        )
    return CheckResult.pass_()


def check_consecutive_losses(
    closed_trades: list[Any],
    max_consecutive_losses: int = 4,
) -> CheckResult:
    """Determine if the last N trades (where N = max_consecutive_losses) were all losses.

    If so, halt all trading (returns a failed CheckResult).
    """
    if len(closed_trades) < max_consecutive_losses:
        return CheckResult.pass_()

    # Sort trades chronologically
    def sort_key(t: Any) -> tuple[int, Any]:
        # Prefer absolute UTC timestamps for cross-asset correctness
        ts = _get_field(t, ["exit_ts", "closed_at", "exit_time", "timestamp", "ts"])
        if ts is not None:
            return (0, ts)
        bar_idx = _get_field(
            t,
            [
                "exit_bar_index",
                "closed_bar_index",
                "exit_index",
                "closed_at_index",
                "exit_bar",
                "bar_index",
            ],
        )
        if bar_idx is not None:
            return (1, int(bar_idx))
        return (2, 0)

    sorted_trades = sorted(closed_trades, key=sort_key)
    recent_trades = sorted_trades[-max_consecutive_losses:]

    losses = []
    for t in recent_trades:
        pnl = _get_field(t, ["net_pnl", "realized_pnl", "pnl"])
        if pnl is not None and Decimal(str(pnl)) < 0:
            losses.append(True)
        else:
            losses.append(False)

    if all(losses):
        return CheckResult.fail(
            f"Consecutive losses limit reached: last {max_consecutive_losses} trades were all losses."
        )

    return CheckResult.pass_()


async def check_and_apply_consecutive_losses(
    db: Database,
    closed_trades: list[Any],
    max_consecutive_losses: int = 4,
) -> CheckResult:
    """Determine if the last N trades were all losses and if so halt trading in DB."""
    res = check_consecutive_losses(closed_trades, max_consecutive_losses)
    if not res.ok:
        await db.set_status(SystemStatusEnum.HALTED, reason=res.reason)
    return res


def check_account_daily_stop(
    daily_realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    starting_equity: Decimal,
    daily_loss_stop_pct: Decimal = Decimal("0.02"),
) -> CheckResult:
    """Determine if account daily loss (realized + unrealized) <= -daily_loss_stop_pct of starting equity."""
    daily_total = Decimal(str(daily_realized_pnl)) + Decimal(str(unrealized_pnl))
    stop_limit = Decimal(str(daily_loss_stop_pct)) * Decimal(str(starting_equity))
    if daily_total <= -stop_limit:
        return CheckResult.fail(
            f"Account daily loss stop hit: total daily PnL {daily_total} <= limit -{stop_limit} "
            f"({daily_loss_stop_pct * 100}% of starting equity {starting_equity})"
        )
    return CheckResult.pass_()


def check_account_cumulative_stop(
    cumulative_realized_pnl: Decimal,
    starting_equity: Decimal,
    cumulative_loss_stop_pct: Decimal = Decimal("0.10"),
) -> CheckResult:
    """Determine if account cumulative loss <= -cumulative_loss_stop_pct of starting equity."""
    stop_limit = Decimal(str(cumulative_loss_stop_pct)) * Decimal(str(starting_equity))
    if Decimal(str(cumulative_realized_pnl)) <= -stop_limit:
        return CheckResult.fail(
            f"Account cumulative loss stop hit: cumulative realized PnL {cumulative_realized_pnl} <= limit -{stop_limit} "
            f"({cumulative_loss_stop_pct * 100}% of starting equity {starting_equity})"
        )
    return CheckResult.pass_()
