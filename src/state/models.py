"""SQLAlchemy models for persistent state.

Single source of truth for positions, orders, fills, funding payments,
and system status. Always queried against the database — never trust
in-memory mirrors past a process restart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    DECIMAL,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Leg(str, Enum):
    SPOT = "spot"
    PERP = "perp"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class SystemStatusEnum(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    HALTED = "halted"


class Position(Base):
    """A delta-neutral pair position (spot long + perp short)."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[PositionStatus] = mapped_column(
        SAEnum(PositionStatus), default=PositionStatus.OPEN
    )
    spot_qty: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    perp_qty: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    spot_entry_price: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    perp_entry_price: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    initial_margin: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    funding_collected: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))

    orders: Mapped[list[Order]] = relationship(back_populates="position")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    leg: Mapped[Leg] = mapped_column(SAEnum(Leg))
    side: Mapped[Side] = mapped_column(SAEnum(Side))
    qty: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    price: Mapped[Decimal | None] = mapped_column(DECIMAL(28, 12), nullable=True)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.NEW)
    filled_qty: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    avg_fill_price: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    fee_paid: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_update_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    position: Mapped[Position] = relationship(back_populates="orders")
    fills: Mapped[list[Fill]] = relationship(back_populates="order")


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    exchange_trade_id: Mapped[str] = mapped_column(String(64), index=True)
    qty: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    price: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    fee: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    fee_asset: Mapped[str] = mapped_column(String(16))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="fills")


class FundingPayment(Base):
    __tablename__ = "funding_payments"
    __table_args__ = (
        UniqueConstraint("symbol", "funding_time", name="uq_funding_symbol_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    funding_rate: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    notional: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    payment: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))  # positive = bot received
    mark_price: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))


class StateSnapshot(Base):
    """Periodic snapshot of equity and exposures for reporting."""

    __tablename__ = "state_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    equity_usdt: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    spot_balance_usdt: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    perp_balance_usdt: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    unrealized_pnl: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    realized_pnl_daily: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))
    realized_pnl_cumulative: Mapped[Decimal] = mapped_column(DECIMAL(28, 12))


class SystemStatus(Base):
    """Single-row table tracking active/paused/halted and last reconciliation."""

    __tablename__ = "system_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    status: Mapped[SystemStatusEnum] = mapped_column(
        SAEnum(SystemStatusEnum), default=SystemStatusEnum.ACTIVE
    )
    halt_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    strategy_meta: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    last_reconciliation_ok: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_update: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    starting_equity: Mapped[Decimal] = mapped_column(DECIMAL(28, 12), default=Decimal("0"))
