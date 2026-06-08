"""Abstract exchange adapter.

The bot speaks to the world only through this interface. New venues
(Bybit, OKX) plug in as another subclass without touching strategy or
risk modules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

Side = Literal["buy", "sell"]
Leg = Literal["spot", "perp", "margin"]


@dataclass(slots=True)
class BorrowInfo:
    """Live cross-margin borrow snapshot for one asset."""

    asset: str
    borrowed: Decimal           # outstanding principal
    interest_accrued: Decimal   # interest since last repayment
    borrow_rate_apr: Decimal    # current live rate (annualised, e.g. 0.06 for 6%)


@dataclass(slots=True)
class MarginAccount:
    """Cross-margin account-level snapshot. `margin_level` is the
    Binance ratio (total assets / total liabilities). Liquidation
    happens around 1.1; our risk overlay closes well above that."""

    total_asset_value: Decimal       # in USDT
    total_liability_value: Decimal   # in USDT (borrows + accrued interest)
    margin_level: Decimal            # asset / liability
    balances: dict[str, Balance]     # margin-account balances by asset


@dataclass(slots=True)
class Ticker:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    ts: datetime


@dataclass(slots=True)
class FundingRate:
    symbol: str
    rate: Decimal  # per 8h period
    next_funding_time: datetime
    mark_price: Decimal


@dataclass(slots=True)
class Balance:
    asset: str
    free: Decimal
    used: Decimal
    total: Decimal


@dataclass(slots=True)
class ExchangePosition:
    symbol: str
    leg: Leg
    qty: Decimal  # signed: positive=long, negative=short
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None
    margin: Decimal
    unrealized_pnl: Decimal


@dataclass(slots=True)
class ExchangeOrder:
    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    leg: Leg
    side: Side
    qty: Decimal
    filled_qty: Decimal
    avg_price: Decimal
    status: str
    fee_paid: Decimal
    fee_asset: str
    ts: datetime


class ExchangeAdapter(ABC):
    """Minimal surface for what the bot needs from a venue."""

    # ---- lifecycle ----------------------------------------------------
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ---- account ------------------------------------------------------
    @abstractmethod
    async def fetch_balances(self) -> dict[str, Balance]: ...

    @abstractmethod
    async def fetch_positions(self) -> list[ExchangePosition]: ...

    @abstractmethod
    async def fetch_server_time(self) -> int:
        """Server epoch in milliseconds."""

    # ---- market data --------------------------------------------------
    @abstractmethod
    async def fetch_ticker(self, symbol: str, leg: Leg) -> Ticker: ...

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> FundingRate: ...

    @abstractmethod
    async def fetch_mark_price(self, symbol: str) -> Decimal: ...

    # ---- trading ------------------------------------------------------
    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...

    @abstractmethod
    async def submit_order(
        self,
        symbol: str,
        leg: Leg,
        side: Side,
        qty: Decimal,
        client_order_id: str,
        price: Decimal | None = None,
        reduce_only: bool = False,
    ) -> ExchangeOrder: ...

    @abstractmethod
    async def fetch_order(
        self, client_order_id: str, symbol: str, leg: Leg
    ) -> ExchangeOrder | None: ...

    @abstractmethod
    async def cancel_order(self, client_order_id: str, symbol: str, leg: Leg) -> None: ...

    # ---- margin -------------------------------------------------------
    @abstractmethod
    async def add_margin(self, symbol: str, amount: Decimal) -> None: ...

    # ---- cross-margin borrow/repay (two-sided carry, negative leg) ----
    #
    # Default implementations raise NotImplementedError so existing
    # adapters that don't support margin (the FakeExchange for the
    # positive-only daemon) keep their narrower contract intact unless
    # they explicitly opt in. The new BinanceAdapter methods and a
    # margin-capable FakeExchange override these.

    async def borrow(self, asset: str, amount: Decimal) -> None:
        """Borrow `amount` of `asset` on the cross-margin account.

        On failure (no inventory, rate spike, exchange refusal) the
        execution engine MUST pre-flight this and abort the perp leg
        cleanly. The strategy spec requires atomic two-leg open."""
        raise NotImplementedError("adapter does not support margin borrow")

    async def repay(self, asset: str, amount: Decimal) -> None:
        """Repay `amount` of `asset` on the cross-margin account."""
        raise NotImplementedError("adapter does not support margin repay")

    async def fetch_borrow_rate(self, asset: str) -> Decimal:
        """Live cross-margin borrow rate for `asset`, as APR (e.g.
        Decimal('0.06') for 6 %)."""
        raise NotImplementedError("adapter does not expose borrow rates")

    async def fetch_borrow_info(self, asset: str) -> BorrowInfo:
        """Per-asset outstanding borrow, accrued interest, current rate."""
        raise NotImplementedError("adapter does not expose borrow info")

    async def fetch_margin_account(self) -> MarginAccount:
        """Cross-margin account snapshot incl. margin_level."""
        raise NotImplementedError("adapter does not expose margin account")
