"""Paper-trading adapter: real Binance prices, fake balances.

Reads tickers and OHLCV from Binance's public REST endpoints (no API
key required, no orders ever submitted), but tracks balances and
fills in memory like `FakeExchange`. Lets the SimpleBot strategy be
evaluated against actual market conditions without putting capital
at risk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import ccxt.async_support as ccxt

from src.adapters.exchange_base import FundingRate, Leg, Ticker
from src.adapters.fake import FakeExchange


class PaperBinanceAdapter(FakeExchange):
    """FakeExchange that fetches real prices/OHLCV from Binance public API."""

    def __init__(
        self,
        starting_usdt: Decimal = Decimal("1000"),
        slippage_bps: Decimal = Decimal("2.0"),
        fee_bps: Decimal = Decimal("4.0"),
    ):
        super().__init__(starting_usdt=starting_usdt, slippage_bps=slippage_bps, fee_bps=fee_bps)
        # Public-only ccxt clients — no API key.
        self.spot = ccxt.binance({"enableRateLimit": True})
        self.perp = ccxt.binanceusdm({"enableRateLimit": True})

    async def connect(self) -> None:
        await self.spot.load_markets()

    async def close(self) -> None:
        try:
            await self.spot.close()
        finally:
            await self.perp.close()

    async def fetch_ticker(self, symbol: str, leg: Leg) -> Ticker:
        client = self.spot if leg == "spot" else self.perp
        raw = await client.fetch_ticker(symbol)
        ticker = Ticker(
            symbol=symbol,
            bid=Decimal(str(raw.get("bid") or raw.get("last") or 0)),
            ask=Decimal(str(raw.get("ask") or raw.get("last") or 0)),
            last=Decimal(str(raw.get("last") or 0)),
            ts=datetime.now(UTC),
        )
        # Cache the fresh ticker in the FakeExchange's dict so order fills
        # use it as the reference price.
        self._tickers[(symbol, leg)] = ticker
        return ticker

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        raw = await self.perp.fetch_funding_rate(symbol)
        return FundingRate(
            symbol=symbol,
            rate=Decimal(str(raw.get("fundingRate") or 0)),
            next_funding_time=datetime.fromtimestamp(
                (raw.get("nextFundingTimestamp") or 0) / 1000, tz=UTC
            ),
            mark_price=Decimal(str(raw.get("markPrice") or 0)),
        )

    async def fetch_mark_price(self, symbol: str) -> Decimal:
        return (await self.fetch_funding_rate(symbol)).mark_price
