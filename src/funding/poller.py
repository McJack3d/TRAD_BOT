"""Funding-payment poller.

Polls the exchange for funding payments accrued since the last recorded
event and persists them. Idempotent: the (symbol, funding_time)
uniqueness constraint in `FundingPayment` blocks duplicates.

Runs as an asyncio task driven every `interval_seconds` (default 5min).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from src.adapters.exchange_base import ExchangeAdapter
from src.config import SymbolConfig
from src.data import MarketData
from src.logging_setup import log
from src.state import Database
from src.state.models import FundingPayment


class FundingPoller:
    def __init__(
        self,
        db: Database,
        exchange: ExchangeAdapter,
        market_data: MarketData,
        symbols: list[SymbolConfig],
        interval_seconds: int = 300,
    ):
        self.db = db
        self.exchange = exchange
        self.market_data = market_data
        self.symbols = symbols
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("funding.poller.error", error=str(e))
            await asyncio.sleep(self.interval_seconds)

    async def tick(self) -> None:
        """Record a funding payment for each open position whose 8h cycle
        boundary has elapsed since the last recorded one.

        Conservative: we record a payment row at the moment the live
        funding rate ticks over to a new `next_funding_time`. The notional
        used is the current spot mid × spot qty so the row is
        self-consistent with the position state.
        """
        positions = await self.db.open_positions()
        if not positions:
            return
        for pos in positions:
            snap = self.market_data.snapshots.get(pos.symbol)
            if snap is None or snap.next_funding_time is None:
                continue
            # Funding settled when wall clock has passed the previous "next" time.
            settled_at = snap.next_funding_time - timedelta(hours=8)
            if settled_at > datetime.now(UTC):
                continue
            notional = abs(pos.perp_qty) * snap.mark_price
            payment = notional * snap.funding_rate  # short receives positive funding
            row = FundingPayment(
                position_id=pos.id,
                symbol=pos.symbol,
                funding_time=settled_at,
                funding_rate=snap.funding_rate,
                notional=notional,
                payment=payment,
                mark_price=snap.mark_price,
            )
            inserted = await self.db.add_funding_payment(row)
            if inserted:
                log.info(
                    "funding.poller.recorded",
                    symbol=pos.symbol,
                    payment=str(payment),
                    rate=str(snap.funding_rate),
                )


