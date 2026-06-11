"""Failure-path tests for the execution engine.

These guard the invariants that matter with real money on the line:
- A position is never marked CLOSED in the DB unless both closing legs
  actually executed (a failed leg used to record garbage PnL at price 0).
- When an order's outcome cannot be determined (submit raised AND the
  recovery fetches failed), the system halts instead of guessing — and
  open_pair does NOT auto-unwind the spot leg, which could leave a naked
  perp short.
- Realized PnL accounts for USDT trading fees on all four legs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.adapters.fake import FakeExchange
from src.config import BotConfig, Mode, SymbolConfig
from src.execution.engine import ExecutionEngine
from src.state.db import Database
from src.state.models import OrderStatus, PositionStatus, SystemStatusEnum


class UnreachableExchange(FakeExchange):
    """Submit raises and the recovery fetch also raises — the order's
    true state is unknowable."""

    def __init__(self, *args, fail_legs: set[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_legs = fail_legs if fail_legs is not None else {"spot", "perp"}

    async def submit_order(self, symbol, leg, side, qty, client_order_id, price=None, reduce_only=False):
        if leg in self.fail_legs:
            raise ConnectionError("network down")
        return await super().submit_order(symbol, leg, side, qty, client_order_id, price, reduce_only)

    async def fetch_order(self, client_order_id, symbol, leg):
        if leg in self.fail_legs:
            raise ConnectionError("network still down")
        return await super().fetch_order(client_order_id, symbol, leg)


class RejectingExchange(FakeExchange):
    """Submit raises but the recovery fetch works and confirms the order
    never landed."""

    def __init__(self, *args, fail_legs: set[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_legs = fail_legs if fail_legs is not None else {"spot", "perp"}

    async def submit_order(self, symbol, leg, side, qty, client_order_id, price=None, reduce_only=False):
        if leg in self.fail_legs:
            raise ConnectionError("rejected at the gate")
        return await super().submit_order(symbol, leg, side, qty, client_order_id, price, reduce_only)


def _cfg() -> BotConfig:
    return BotConfig(
        mode=Mode.PAPER,
        starting_equity_eur=Decimal("1000"),
        symbols=[
            SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT", min_qty=Decimal("0.00001"))
        ],
    )


def _seed_prices(ex: FakeExchange) -> None:
    ex.set_ticker("BTC/USDT", "spot", Decimal("30000"))
    ex.set_ticker("BTC/USDT:USDT", "perp", Decimal("30010"))


async def _db(tmp_path: Path) -> Database:
    d = Database(str(tmp_path / "engine.db"))
    await d.init(starting_equity=Decimal("1000"))
    return d


async def _open_position(db: Database, ex: FakeExchange):
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=ex, dry_run=False)
    pos = await engine.open_pair("BTC/USDT", Decimal("300"))
    assert pos is not None
    return pos


@pytest.mark.asyncio
async def test_close_pair_keeps_position_open_when_close_leg_rejected(tmp_path):
    """A definitively-failed closing leg must halt, not record a close at price 0."""
    db = await _db(tmp_path)
    good_ex = FakeExchange(starting_usdt=Decimal("4000"))
    _seed_prices(good_ex)
    pos = await _open_position(db, good_ex)

    bad_ex = RejectingExchange(starting_usdt=Decimal("4000"), fail_legs={"perp"})
    _seed_prices(bad_ex)
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=bad_ex, dry_run=False)
    await engine.close_pair(pos.id, reason="test")

    refreshed = await db.get_position(pos.id)
    assert refreshed.status == PositionStatus.OPEN
    assert refreshed.realized_pnl == Decimal("0")
    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED
    # The spot leg must not have been sold after the perp close failed —
    # that would have left a naked perp short.
    sides = [(o.leg, o.side) for o in bad_ex._orders.values()]
    assert ("spot", "sell") not in sides


@pytest.mark.asyncio
async def test_close_pair_state_unknown_halts_without_closing(tmp_path):
    """If the exchange is unreachable mid-close, leave the position open and halt."""
    db = await _db(tmp_path)
    good_ex = FakeExchange(starting_usdt=Decimal("4000"))
    _seed_prices(good_ex)
    pos = await _open_position(db, good_ex)

    dead_ex = UnreachableExchange(starting_usdt=Decimal("4000"))
    _seed_prices(dead_ex)
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=dead_ex, dry_run=False)
    await engine.close_pair(pos.id, reason="test")

    refreshed = await db.get_position(pos.id)
    assert refreshed.status == PositionStatus.OPEN
    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED


@pytest.mark.asyncio
async def test_open_pair_perp_unknown_holds_spot_and_halts(tmp_path):
    """Unknown perp-leg outcome: do NOT auto-sell the spot (the short may
    exist on the exchange) — halt for human reconciliation instead."""
    db = await _db(tmp_path)
    ex = UnreachableExchange(starting_usdt=Decimal("4000"), fail_legs={"perp"})
    _seed_prices(ex)
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=ex, dry_run=False)

    pos = await engine.open_pair("BTC/USDT", Decimal("300"))
    assert pos is None

    sides = [(o.leg, o.side) for o in ex._orders.values()]
    assert ("spot", "buy") in sides
    assert ("spot", "sell") not in sides  # spot leg was NOT unwound
    status = await db.get_status()
    assert status.status == SystemStatusEnum.HALTED


@pytest.mark.asyncio
async def test_open_pair_perp_rejected_unwinds_spot(tmp_path):
    """Definitively-rejected perp leg: the spot buy is unwound as before."""
    db = await _db(tmp_path)
    ex = RejectingExchange(starting_usdt=Decimal("4000"), fail_legs={"perp"})
    _seed_prices(ex)
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=ex, dry_run=False)

    pos = await engine.open_pair("BTC/USDT", Decimal("300"))
    assert pos is None

    sides = [(o.leg, o.side) for o in ex._orders.values()]
    assert ("spot", "buy") in sides
    assert ("spot", "sell") in sides  # unwind happened


@pytest.mark.asyncio
async def test_submit_failure_marks_order_unknown(tmp_path):
    """The unresolved order row is flagged UNKNOWN so the operator can
    find exactly which order needs manual reconciliation."""
    db = await _db(tmp_path)
    ex = UnreachableExchange(starting_usdt=Decimal("4000"), fail_legs={"spot"})
    _seed_prices(ex)
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=ex, dry_run=False)

    pos = await engine.open_pair("BTC/USDT", Decimal("300"))
    assert pos is None
    orders = await db.recent_orders(datetime.now(UTC) - timedelta(hours=1))
    assert any(o.status == OrderStatus.UNKNOWN for o in orders)


@pytest.mark.asyncio
async def test_close_pair_realized_pnl_includes_fees(tmp_path):
    """Realized PnL = leg PnL + funding − all USDT fees (entry and exit)."""
    db = await _db(tmp_path)
    ex = FakeExchange(starting_usdt=Decimal("4000"))
    _seed_prices(ex)
    pos = await _open_position(db, ex)
    engine = ExecutionEngine(cfg=_cfg(), db=db, exchange=ex, dry_run=False)
    await engine.close_pair(pos.id, reason="test")

    refreshed = await db.get_position(pos.id)
    assert refreshed.status == PositionStatus.CLOSED

    orders = {(o.leg, o.side): o for o in ex._orders.values()}
    spot_entry = orders[("spot", "buy")]
    spot_exit = orders[("spot", "sell")]
    perp_entry = orders[("perp", "sell")]
    perp_exit = orders[("perp", "buy")]

    spot_pnl = (spot_exit.avg_price - spot_entry.avg_price) * pos.spot_qty
    perp_pnl = (perp_entry.avg_price - perp_exit.avg_price) * abs(pos.perp_qty)
    total_fees = sum(
        o.fee_paid for o in (spot_entry, spot_exit, perp_entry, perp_exit)
    )
    assert total_fees > 0
    expected = spot_pnl + perp_pnl - total_fees
    assert refreshed.realized_pnl == expected
