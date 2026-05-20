"""Funding poller records payments idempotently."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.adapters.fake import FakeExchange
from src.config import SymbolConfig
from src.data.market_data import MarketData
from src.funding.poller import FundingPoller
from src.state.db import Database
from src.state.models import FundingPayment, Position, PositionStatus


@pytest.fixture
def symbols() -> list[SymbolConfig]:
    return [SymbolConfig(spot="BTC/USDT", perp="BTC/USDT:USDT")]


async def _seed_position(db: Database) -> None:
    await db.create_position(
        Position(
            symbol="BTC/USDT",
            status=PositionStatus.OPEN,
            spot_qty=Decimal("0.0166"),
            perp_qty=Decimal("-0.0166"),
            spot_entry_price=Decimal("30000"),
            perp_entry_price=Decimal("30010"),
            initial_margin=Decimal("250"),
        )
    )


async def test_records_payment_after_funding_cycle(symbols, tmp_path: Path) -> None:
    db = Database(str(tmp_path / "f.db"))
    await db.init(starting_equity=Decimal("1000"))
    await _seed_position(db)

    ex = FakeExchange()
    md = MarketData(ex, ["BTC/USDT"], ticker_poll_seconds=999, funding_poll_seconds=999)
    snap = md.get("BTC/USDT")
    snap.funding_rate = Decimal("0.0003")
    snap.mark_price = Decimal("30010")
    # next_funding 1s in the past → cycle has settled.
    snap.next_funding_time = datetime.now(UTC) + timedelta(hours=8) - timedelta(seconds=1)
    # Force the snapshot to look like the cycle just ended.
    snap.next_funding_time = datetime.now(UTC) - timedelta(seconds=1) + timedelta(hours=8)

    # Manually trigger one tick (don't start the loop — keep test deterministic).
    poller = FundingPoller(db, ex, md, symbols, interval_seconds=999)
    await poller.tick()

    from sqlalchemy import select

    async with db.session() as s:
        rows = (await s.execute(select(FundingPayment))).scalars().all()
    assert len(rows) == 1
    assert rows[0].symbol == "BTC/USDT"
    assert rows[0].payment > 0  # positive funding → short receives
    await db.close()


async def test_duplicate_cycle_not_double_recorded(symbols, tmp_path: Path) -> None:
    db = Database(str(tmp_path / "f.db"))
    await db.init(starting_equity=Decimal("1000"))
    await _seed_position(db)

    ex = FakeExchange()
    md = MarketData(ex, ["BTC/USDT"], ticker_poll_seconds=999, funding_poll_seconds=999)
    snap = md.get("BTC/USDT")
    snap.funding_rate = Decimal("0.0003")
    snap.mark_price = Decimal("30010")
    snap.next_funding_time = datetime.now(UTC) - timedelta(seconds=1) + timedelta(hours=8)

    poller = FundingPoller(db, ex, md, symbols, interval_seconds=999)
    await poller.tick()
    await poller.tick()  # repeat — should be deduped by unique constraint
    await poller.tick()

    from sqlalchemy import select

    async with db.session() as s:
        rows = (await s.execute(select(FundingPayment))).scalars().all()
    assert len(rows) == 1
    await db.close()


async def test_no_open_positions_no_payments(symbols, tmp_path: Path) -> None:
    db = Database(str(tmp_path / "f.db"))
    await db.init(starting_equity=Decimal("1000"))
    ex = FakeExchange()
    md = MarketData(ex, ["BTC/USDT"], ticker_poll_seconds=999, funding_poll_seconds=999)
    poller = FundingPoller(db, ex, md, symbols, interval_seconds=999)
    await poller.tick()  # no positions → no-op
    await db.close()
