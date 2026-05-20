"""Basic DB sanity tests (in-memory SQLite, async)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.state.models import (
    FundingPayment,
    Leg,
    Order,
    Position,
    Side,
    SystemStatusEnum,
)


async def test_init_creates_system_status(db) -> None:
    status = await db.get_status()
    assert status.status == SystemStatusEnum.ACTIVE
    assert status.starting_equity == Decimal("1000")


async def test_open_and_close_position(db) -> None:
    p = await db.create_position(
        Position(
            symbol="BTC/USDT",
            spot_qty=Decimal("0.01"),
            perp_qty=Decimal("-0.01"),
            spot_entry_price=Decimal("30000"),
            perp_entry_price=Decimal("30050"),
            initial_margin=Decimal("150"),
        )
    )
    open_ = await db.open_positions()
    assert len(open_) == 1
    await db.close_position(p.id, realized_pnl=Decimal("5"))
    open_after = await db.open_positions()
    assert open_after == []


async def test_order_idempotency(db) -> None:
    cid = "test-1234"
    await db.add_order(Order(client_order_id=cid, symbol="BTC/USDT", leg=Leg.SPOT, side=Side.BUY, qty=Decimal("1")))
    found = await db.get_order_by_client_id(cid)
    assert found is not None and found.client_order_id == cid


async def test_duplicate_funding_payment_returns_none(db) -> None:
    ts = datetime.now(UTC)
    fp = FundingPayment(
        symbol="BTC/USDT",
        funding_time=ts,
        funding_rate=Decimal("0.0002"),
        notional=Decimal("100"),
        payment=Decimal("0.02"),
        mark_price=Decimal("30000"),
    )
    first = await db.add_funding_payment(fp)
    assert first is not None
    dup = FundingPayment(
        symbol="BTC/USDT",
        funding_time=ts,
        funding_rate=Decimal("0.0002"),
        notional=Decimal("100"),
        payment=Decimal("0.02"),
        mark_price=Decimal("30000"),
    )
    again = await db.add_funding_payment(dup)
    assert again is None


async def test_order_rate_window(db) -> None:
    for i in range(3):
        await db.add_order(
            Order(
                client_order_id=f"t-{i}",
                symbol="BTC/USDT",
                leg=Leg.SPOT,
                side=Side.BUY,
                qty=Decimal("1"),
            )
        )
    count = await db.order_count_in_window(60)
    assert count == 3
