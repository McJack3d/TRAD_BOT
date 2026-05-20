"""Pure signal evaluation.

Stateless per-tick — takes the current snapshot plus the optional
position state and returns Entry / Exit / Hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Union

from src.config import StrategyConfig


@dataclass(slots=True)
class EntrySignal:
    symbol: str
    notional: Decimal
    reason: str


@dataclass(slots=True)
class ExitSignal:
    symbol: str
    reason: str


@dataclass(slots=True)
class HoldSignal:
    symbol: str


Signal = Union[EntrySignal, ExitSignal, HoldSignal]


@dataclass(slots=True)
class PositionView:
    """Minimal position info the strategy needs."""

    symbol: str
    opened_at: datetime
    notional: Decimal


def evaluate_signal(
    symbol: str,
    funding_rate: Decimal,
    cfg: StrategyConfig,
    position: PositionView | None,
    proposed_notional: Decimal,
    now: datetime | None = None,
) -> Signal:
    now = now or datetime.now(UTC)

    if position is None:
        if funding_rate >= cfg.entry_funding_threshold:
            return EntrySignal(
                symbol=symbol,
                notional=proposed_notional,
                reason=f"funding {funding_rate} >= entry threshold {cfg.entry_funding_threshold}",
            )
        return HoldSignal(symbol=symbol)

    # Position is open: respect min dwell, then check exit threshold.
    dwell = now - position.opened_at
    if dwell < timedelta(hours=cfg.min_dwell_hours):
        return HoldSignal(symbol=symbol)
    if funding_rate <= cfg.exit_funding_threshold:
        return ExitSignal(
            symbol=symbol,
            reason=f"funding {funding_rate} <= exit threshold {cfg.exit_funding_threshold}",
        )
    return HoldSignal(symbol=symbol)
