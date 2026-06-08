"""Live market data container and poller.

Maintains in-memory snapshots of spot and perp prices, mark prices,
and funding rates. Serves as a REST-based fallback and base cache for
WebSocket updates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.adapters.exchange_base import ExchangeAdapter
from src.logging_setup import log


@dataclass
class MarketSnapshot:
    symbol: str
    spot_bid: Decimal = Decimal("0")
    spot_ask: Decimal = Decimal("0")
    perp_bid: Decimal = Decimal("0")
    perp_ask: Decimal = Decimal("0")
    mark_price: Decimal = Decimal("0")
    funding_rate: Decimal = Decimal("0")
    next_funding_time: datetime | None = None

    @property
    def spot_mid(self) -> Decimal:
        return (self.spot_bid + self.spot_ask) / Decimal("2")

    @property
    def perp_mid(self) -> Decimal:
        return (self.perp_bid + self.perp_ask) / Decimal("2")


class MarketData:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        symbols: list[str],
        ticker_poll_seconds: int = 5,
        funding_poll_seconds: int = 60,
    ):
        self.exchange = exchange
        self.symbols = symbols
        self.ticker_poll_seconds = ticker_poll_seconds
        self.funding_poll_seconds = funding_poll_seconds

        self.snapshots: dict[str, MarketSnapshot] = {
            s: MarketSnapshot(symbol=s) for s in symbols
        }

        self._ticker_task: asyncio.Task | None = None
        self._funding_task: asyncio.Task | None = None

    def get(self, symbol: str) -> MarketSnapshot:
        """Get the current snapshot for a symbol. Returns a new empty snapshot
        if the symbol is not pre-registered.
        """
        if symbol not in self.snapshots:
            self.snapshots[symbol] = MarketSnapshot(symbol=symbol)
        return self.snapshots[symbol]

    async def start(self) -> None:
        """Start the background polling loops."""
        if self._ticker_task is None or self._ticker_task.done():
            self._ticker_task = asyncio.create_task(self._ticker_loop())
        if self._funding_task is None or self._funding_task.done():
            self._funding_task = asyncio.create_task(self._funding_loop())
        log.info("market_data.poller.started")

    async def stop(self) -> None:
        """Stop the background polling loops."""
        if self._ticker_task:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except asyncio.CancelledError:
                pass
            self._ticker_task = None
        if self._funding_task:
            self._funding_task.cancel()
            try:
                await self._funding_task
            except asyncio.CancelledError:
                pass
            self._funding_task = None
        log.info("market_data.poller.stopped")

    def _perp_symbol(self, symbol: str) -> str:
        """Map a spot symbol (e.g. BTC/USDT) to ccxt's perp representation (BTC/USDT:USDT)."""
        return symbol if ":" in symbol else f"{symbol}:{symbol.split('/')[1]}"

    async def _ticker_loop(self) -> None:
        while True:
            try:
                await self.poll_tickers()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("market_data.ticker_loop.error", error=str(e))
            await asyncio.sleep(self.ticker_poll_seconds)

    async def _funding_loop(self) -> None:
        while True:
            try:
                await self.poll_funding()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("market_data.funding_loop.error", error=str(e))
            await asyncio.sleep(self.funding_poll_seconds)

    async def poll_tickers(self) -> None:
        """Fetch spot and perp tickers for all configured symbols."""
        tasks = []
        for symbol in self.symbols:
            tasks.append(self._poll_single_ticker(symbol))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_single_ticker(self, symbol: str) -> None:
        # Fetch spot ticker
        try:
            spot_ticker = await self.exchange.fetch_ticker(symbol, "spot")
            snap = self.get(symbol)
            snap.spot_bid = spot_ticker.bid
            snap.spot_ask = spot_ticker.ask
        except Exception as e:
            log.warning("market_data.fetch_spot_ticker.failed", symbol=symbol, error=str(e))

        # Fetch perp ticker
        try:
            perp_symbol = self._perp_symbol(symbol)
            perp_ticker = await self.exchange.fetch_ticker(perp_symbol, "perp")
            snap = self.get(symbol)
            snap.perp_bid = perp_ticker.bid
            snap.perp_ask = perp_ticker.ask
        except Exception as e:
            log.warning("market_data.fetch_perp_ticker.failed", symbol=symbol, error=str(e))

    async def poll_funding(self) -> None:
        """Fetch perp mark price and funding rate for all configured symbols."""
        tasks = []
        for symbol in self.symbols:
            tasks.append(self._poll_single_funding(symbol))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_single_funding(self, symbol: str) -> None:
        try:
            perp_symbol = self._perp_symbol(symbol)
            funding = await self.exchange.fetch_funding_rate(perp_symbol)
            snap = self.get(symbol)
            snap.funding_rate = funding.rate
            snap.next_funding_time = funding.next_funding_time
            snap.mark_price = funding.mark_price
        except Exception as e:
            log.warning("market_data.fetch_funding.failed", symbol=symbol, error=str(e))
