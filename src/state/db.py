"""Async SQLite database wrapper with helpful DAO-style methods."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.state.models import (
    Base,
    Fill,
    FundingPayment,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    StateSnapshot,
    SystemStatus,
    SystemStatusEnum,
)


class Database:
    def __init__(self, path: str = "data/bot.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.url = f"sqlite+aiosqlite:///{path}"
        self.engine = create_async_engine(self.url, echo=False, future=True)
        self._session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def init(self, starting_equity: Decimal | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self._session() as s:
            status = await s.get(SystemStatus, 1)
            if status is None:
                s.add(
                    SystemStatus(
                        id=1,
                        status=SystemStatusEnum.ACTIVE,
                        starting_equity=starting_equity or Decimal("0"),
                    )
                )
                await s.commit()

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session() as s:
            yield s

    # ---- system status ------------------------------------------------

    async def get_status(self) -> SystemStatus:
        async with self._session() as s:
            status = await s.get(SystemStatus, 1)
            if status is None:
                raise RuntimeError("system_status row missing; call db.init() first")
            return status

    async def set_status(
        self, status: SystemStatusEnum, reason: str | None = None
    ) -> None:
        async with self._session() as s:
            await s.execute(
                update(SystemStatus)
                .where(SystemStatus.id == 1)
                .values(
                    status=status,
                    halt_reason=reason,
                    last_update=datetime.now(UTC),
                )
            )
            await s.commit()

    async def touch_reconciliation_ok(self, ts: datetime | None = None) -> None:
        async with self._session() as s:
            await s.execute(
                update(SystemStatus)
                .where(SystemStatus.id == 1)
                .values(last_reconciliation_ok=ts or datetime.now(UTC))
            )
            await s.commit()

    # ---- positions ----------------------------------------------------

    async def open_positions(self) -> list[Position]:
        async with self._session() as s:
            res = await s.execute(
                select(Position).where(Position.status == PositionStatus.OPEN)
            )
            return list(res.scalars().all())

    async def get_position(self, position_id: int) -> Position | None:
        async with self._session() as s:
            return await s.get(Position, position_id)

    async def create_position(self, position: Position) -> Position:
        async with self._session() as s:
            s.add(position)
            await s.commit()
            await s.refresh(position)
            return position

    async def close_position(
        self, position_id: int, realized_pnl: Decimal
    ) -> None:
        async with self._session() as s:
            await s.execute(
                update(Position)
                .where(Position.id == position_id)
                .values(
                    status=PositionStatus.CLOSED,
                    closed_at=datetime.now(UTC),
                    realized_pnl=realized_pnl,
                )
            )
            await s.commit()

    # ---- orders -------------------------------------------------------

    async def add_order(self, order: Order) -> Order:
        async with self._session() as s:
            s.add(order)
            await s.commit()
            await s.refresh(order)
            return order

    async def update_order_status(
        self,
        client_order_id: str,
        status: OrderStatus,
        filled_qty: Decimal | None = None,
        avg_price: Decimal | None = None,
        exchange_order_id: str | None = None,
        fee_paid: Decimal | None = None,
    ) -> None:
        values: dict = {"status": status, "last_update_at": datetime.now(UTC)}
        if filled_qty is not None:
            values["filled_qty"] = filled_qty
        if avg_price is not None:
            values["avg_fill_price"] = avg_price
        if exchange_order_id is not None:
            values["exchange_order_id"] = exchange_order_id
        if fee_paid is not None:
            values["fee_paid"] = fee_paid
        async with self._session() as s:
            await s.execute(
                update(Order).where(Order.client_order_id == client_order_id).values(**values)
            )
            await s.commit()

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        async with self._session() as s:
            res = await s.execute(
                select(Order).where(Order.client_order_id == client_order_id)
            )
            return res.scalar_one_or_none()

    async def recent_orders(self, since: datetime) -> list[Order]:
        async with self._session() as s:
            res = await s.execute(select(Order).where(Order.submitted_at >= since))
            return list(res.scalars().all())

    async def order_count_in_window(self, window_seconds: int) -> int:
        since = datetime.now(UTC) - timedelta(seconds=window_seconds)
        async with self._session() as s:
            res = await s.execute(select(Order).where(Order.submitted_at >= since))
            return len(list(res.scalars().all()))

    # ---- fills --------------------------------------------------------

    async def add_fill(self, fill: Fill) -> Fill:
        async with self._session() as s:
            s.add(fill)
            await s.commit()
            await s.refresh(fill)
            return fill

    # ---- funding ------------------------------------------------------

    async def add_funding_payment(self, payment: FundingPayment) -> FundingPayment | None:
        """Returns None if a duplicate (symbol, funding_time) row already exists."""
        async with self._session() as s:
            existing = await s.execute(
                select(FundingPayment).where(
                    (FundingPayment.symbol == payment.symbol)
                    & (FundingPayment.funding_time == payment.funding_time)
                )
            )
            if existing.scalar_one_or_none() is not None:
                return None
            s.add(payment)
            await s.commit()
            await s.refresh(payment)
            return payment

    async def total_funding_since(self, since: datetime) -> Decimal:
        async with self._session() as s:
            res = await s.execute(
                select(FundingPayment).where(FundingPayment.funding_time >= since)
            )
            return sum(
                (p.payment for p in res.scalars().all()), start=Decimal("0")
            )

    async def total_funding(self) -> Decimal:
        """All-time funding collected (positive = bot received)."""
        async with self._session() as s:
            res = await s.execute(select(FundingPayment))
            return sum((p.payment for p in res.scalars().all()), start=Decimal("0"))

    # ---- realized PnL -------------------------------------------------

    async def realized_position_pnl_since(self, since: datetime) -> Decimal:
        """Sum of realized PnL from positions CLOSED on/after `since`.

        This is the spot+perp round-trip PnL recorded by
        `close_position`. Funding income is tracked separately via the
        funding-payment table — combine the two for total realized PnL.
        """
        async with self._session() as s:
            res = await s.execute(
                select(Position).where(
                    (Position.status == PositionStatus.CLOSED)
                    & (Position.closed_at.is_not(None))
                    & (Position.closed_at >= since)
                )
            )
            return sum(
                (p.realized_pnl for p in res.scalars().all()), start=Decimal("0")
            )

    async def total_realized_position_pnl(self) -> Decimal:
        """All-time realized PnL from closed positions."""
        async with self._session() as s:
            res = await s.execute(
                select(Position).where(Position.status == PositionStatus.CLOSED)
            )
            return sum(
                (p.realized_pnl for p in res.scalars().all()), start=Decimal("0")
            )

    # ---- snapshots ----------------------------------------------------

    async def add_snapshot(self, snap: StateSnapshot) -> StateSnapshot:
        async with self._session() as s:
            s.add(snap)
            await s.commit()
            await s.refresh(snap)
            return snap

    async def latest_snapshot(self) -> StateSnapshot | None:
        async with self._session() as s:
            res = await s.execute(
                select(StateSnapshot).order_by(StateSnapshot.ts.desc()).limit(1)
            )
            return res.scalar_one_or_none()
