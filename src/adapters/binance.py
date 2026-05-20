"""Binance adapter using ccxt's async client.

Two ccxt instances — `binance` (spot) and `binanceusdm` (USDT-margined
perp). Same API key works for both. Testnet is selected by setting
sandbox mode on each client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import ccxt.async_support as ccxt  # type: ignore[import-untyped]

from src.adapters.exchange_base import (
    Balance,
    ExchangeAdapter,
    ExchangeOrder,
    ExchangePosition,
    FundingRate,
    Leg,
    Side,
    Ticker,
)


def _d(x) -> Decimal:
    if x is None:
        return Decimal("0")
    return Decimal(str(x))


class BinanceAdapter(ExchangeAdapter):
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
    ):
        common = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"adjustForTimeDifference": True},
        }
        self.spot = ccxt.binance({**common, "options": {**common["options"], "defaultType": "spot"}})
        self.perp = ccxt.binanceusdm(common)
        if testnet:
            self.spot.set_sandbox_mode(True)
            self.perp.set_sandbox_mode(True)

    async def connect(self) -> None:
        await self.spot.load_markets()
        await self.perp.load_markets()

    async def close(self) -> None:
        await self.spot.close()
        await self.perp.close()

    def _client(self, leg: Leg):
        return self.spot if leg == "spot" else self.perp

    # ---- account ------------------------------------------------------
    async def fetch_balances(self) -> dict[str, Balance]:
        out: dict[str, Balance] = {}
        for leg, client in (("spot", self.spot), ("perp", self.perp)):
            bal = await client.fetch_balance()
            for asset, info in bal.get("total", {}).items():
                if info is None:
                    continue
                key = f"{leg}:{asset}"
                free = _d(bal["free"].get(asset))
                used = _d(bal["used"].get(asset))
                total = _d(info)
                out[key] = Balance(asset=asset, free=free, used=used, total=total)
        return out

    async def fetch_positions(self) -> list[ExchangePosition]:
        positions: list[ExchangePosition] = []
        perp_raw = await self.perp.fetch_positions()
        for p in perp_raw:
            contracts = _d(p.get("contracts") or 0)
            if contracts == 0:
                continue
            side = p.get("side")
            signed_qty = contracts if side == "long" else -contracts
            positions.append(
                ExchangePosition(
                    symbol=p["symbol"],
                    leg="perp",
                    qty=signed_qty,
                    entry_price=_d(p.get("entryPrice")),
                    mark_price=_d(p.get("markPrice")),
                    liquidation_price=_d(p.get("liquidationPrice"))
                    if p.get("liquidationPrice") is not None
                    else None,
                    margin=_d(p.get("initialMargin") or p.get("collateral") or 0),
                    unrealized_pnl=_d(p.get("unrealizedPnl") or 0),
                )
            )
        return positions

    async def fetch_server_time(self) -> int:
        return int(await self.perp.fetch_time())

    # ---- market data --------------------------------------------------
    async def fetch_ticker(self, symbol: str, leg: Leg) -> Ticker:
        client = self._client(leg)
        t = await client.fetch_ticker(symbol)
        return Ticker(
            symbol=symbol,
            bid=_d(t.get("bid")),
            ask=_d(t.get("ask")),
            last=_d(t.get("last")),
            ts=datetime.fromtimestamp((t.get("timestamp") or 0) / 1000, tz=UTC),
        )

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        f = await self.perp.fetch_funding_rate(symbol)
        return FundingRate(
            symbol=symbol,
            rate=_d(f.get("fundingRate")),
            next_funding_time=datetime.fromtimestamp(
                (f.get("nextFundingTimestamp") or 0) / 1000, tz=UTC
            ),
            mark_price=_d(f.get("markPrice")),
        )

    async def fetch_mark_price(self, symbol: str) -> Decimal:
        f = await self.perp.fetch_funding_rate(symbol)
        return _d(f.get("markPrice"))

    # ---- trading ------------------------------------------------------
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self.perp.set_leverage(leverage, symbol)
        # Force isolated margin mode for short legs.
        try:
            await self.perp.set_margin_mode("ISOLATED", symbol)
        except Exception:
            # ccxt raises if margin mode is already set; ignore.
            pass

    async def submit_order(
        self,
        symbol: str,
        leg: Leg,
        side: Side,
        qty: Decimal,
        client_order_id: str,
        price: Decimal | None = None,
        reduce_only: bool = False,
    ) -> ExchangeOrder:
        client = self._client(leg)
        order_type = "limit" if price is not None else "market"
        params: dict = {"newClientOrderId": client_order_id}
        if reduce_only and leg == "perp":
            params["reduceOnly"] = True
        raw = await client.create_order(
            symbol=symbol,
            type=order_type,
            side=side,
            amount=float(qty),
            price=float(price) if price is not None else None,
            params=params,
        )
        return _to_exchange_order(raw, leg)

    async def fetch_order(
        self, client_order_id: str, symbol: str, leg: Leg
    ) -> ExchangeOrder | None:
        client = self._client(leg)
        try:
            raw = await client.fetch_order(client_order_id, symbol, {"origClientOrderId": client_order_id})
        except ccxt.OrderNotFound:
            return None
        return _to_exchange_order(raw, leg)

    async def cancel_order(self, client_order_id: str, symbol: str, leg: Leg) -> None:
        client = self._client(leg)
        try:
            await client.cancel_order(client_order_id, symbol, {"origClientOrderId": client_order_id})
        except ccxt.OrderNotFound:
            pass

    # ---- margin -------------------------------------------------------
    async def add_margin(self, symbol: str, amount: Decimal) -> None:
        await self.perp.add_margin(symbol, float(amount))


def _to_exchange_order(raw: dict, leg: Leg) -> ExchangeOrder:
    fee = raw.get("fee") or {}
    fees = raw.get("fees") or []
    fee_paid = _d(fee.get("cost"))
    fee_asset = fee.get("currency") or ""
    if fees:
        # Sum fee costs if Binance returned a list.
        fee_paid = sum((_d(f.get("cost")) for f in fees), start=Decimal("0"))
        fee_asset = fees[0].get("currency") or fee_asset
    ts_ms = raw.get("timestamp") or 0
    return ExchangeOrder(
        client_order_id=raw.get("clientOrderId") or "",
        exchange_order_id=str(raw.get("id")) if raw.get("id") is not None else None,
        symbol=raw.get("symbol") or "",
        leg=leg,
        side=raw.get("side") or "buy",
        qty=_d(raw.get("amount")),
        filled_qty=_d(raw.get("filled")),
        avg_price=_d(raw.get("average") or raw.get("price")),
        status=raw.get("status") or "unknown",
        fee_paid=fee_paid,
        fee_asset=fee_asset,
        ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else datetime.now(UTC),
    )
