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
    BorrowInfo,
    ExchangeAdapter,
    ExchangeOrder,
    ExchangePosition,
    FundingRate,
    Leg,
    MarginAccount,
    Side,
    Ticker,
)

# 365 days expressed in milliseconds — used to annualise the period-based
# borrow rate ccxt returns (Binance quotes a *daily* cross-margin rate).
_MS_PER_YEAR = Decimal("31536000000")


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
        self.api_key = api_key
        self.api_secret = api_secret
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
        if self.api_key:
            await self.spot.load_markets()
            await self.perp.load_markets()
        else:
            # Public requests only
            await self.spot.load_markets()
            await self.perp.load_markets()

    async def close(self) -> None:
        await self.spot.close()
        await self.perp.close()

    def _client(self, leg: Leg):
        return self.spot if leg == "spot" else self.perp

    # ---- account ------------------------------------------------------
    async def fetch_balances(self) -> dict[str, Balance]:
        """Fetch spot + perp balances.

        Each leg is queried independently. If one leg fails — most
        commonly the perp/futures account when the API key has no
        futures permission (the trend bot deliberately runs spot-only
        keys) — that leg is skipped and the other still returns. We only
        raise if BOTH legs fail.
        """
        if not self.api_key:
            return {
                "spot:USDT": Balance(asset="USDT", free=Decimal("2000"), used=Decimal("0"), total=Decimal("2000")),
                "perp:USDT": Balance(asset="USDT", free=Decimal("2000"), used=Decimal("0"), total=Decimal("2000")),
            }
        out: dict[str, Balance] = {}
        errors: list[str] = []
        for leg, client in (("spot", self.spot), ("perp", self.perp)):
            try:
                bal = await client.fetch_balance()
            except Exception as e:
                errors.append(f"{leg}: {e}")
                continue
            for asset, info in bal.get("total", {}).items():
                if info is None:
                    continue
                key = f"{leg}:{asset}"
                free = _d(bal["free"].get(asset))
                used = _d(bal["used"].get(asset))
                total = _d(info)
                out[key] = Balance(asset=asset, free=free, used=used, total=total)
        if not out and errors:
            raise RuntimeError(f"all balance legs failed: {'; '.join(errors)}")
        return out

    async def fetch_positions(self) -> list[ExchangePosition]:
        if not self.api_key:
            return []
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

    # ---- cross-margin borrow/repay (two-sided carry, negative leg) ----
    #
    # Borrow/repay go through the *spot* ccxt client because Binance's
    # cross-margin account is part of the spot/margin API surface, not the
    # USDT-M futures one. The perp leg keeps using `self.perp`.

    async def borrow(self, asset: str, amount: Decimal) -> None:
        """Borrow `asset` on the cross-margin account.

        Raises on any exchange refusal (no inventory, rate spike, ratio
        breach) — the execution engine pre-flights this and aborts the
        whole two-leg open cleanly, never leaving the perp leg naked."""
        await self.spot.borrow_cross_margin(asset, float(amount))

    async def repay(self, asset: str, amount: Decimal) -> None:
        """Repay `asset` on the cross-margin account. Binance applies the
        repayment to accrued interest first, then principal."""
        await self.spot.repay_cross_margin(asset, float(amount))

    async def fetch_borrow_rate(self, asset: str) -> Decimal:
        """Live cross-margin borrow rate for `asset`, normalised to APR.

        ccxt's `fetchCrossBorrowRate` returns a rate over a `period`
        (Binance quotes daily, period = 86_400_000 ms). We annualise to a
        single APR figure so the carry math can compare it directly to the
        per-8h funding via `borrow_rate_per_8h`."""
        raw = await self.spot.fetch_cross_borrow_rate(asset)
        rate = _d(raw.get("rate"))
        period_ms = raw.get("period") or 86_400_000
        periods_per_year = _MS_PER_YEAR / Decimal(str(period_ms))
        return rate * periods_per_year

    async def fetch_borrow_info(self, asset: str) -> BorrowInfo:
        """Outstanding principal + accrued interest for `asset`, read from
        the authoritative cross-margin account `userAssets`, plus the live
        annualised rate."""
        raw = await self.spot.fetch_balance({"type": "margin"})
        user_assets = (raw.get("info") or {}).get("userAssets") or []
        borrowed = Decimal("0")
        interest = Decimal("0")
        for ua in user_assets:
            if ua.get("asset") == asset:
                borrowed = _d(ua.get("borrowed"))
                interest = _d(ua.get("interest"))
                break
        rate = await self.fetch_borrow_rate(asset)
        return BorrowInfo(
            asset=asset,
            borrowed=borrowed,
            interest_accrued=interest,
            borrow_rate_apr=rate,
        )

    async def fetch_margin_account(self) -> MarginAccount:
        """Cross-margin account snapshot. `marginLevel` is the live ratio
        the risk overlay gates on (Binance reports a large sentinel when
        there is no debt). Asset/liability totals are reported by Binance
        in BTC; we convert to USDT via the BTC mark for the dollar gate."""
        raw = await self.spot.fetch_balance({"type": "margin"})
        info = raw.get("info") or {}
        margin_level = _d(info.get("marginLevel"))
        total_asset_btc = _d(info.get("totalAssetOfBtc"))
        total_liab_btc = _d(info.get("totalLiabilityOfBtc"))
        btc_usdt = (
            await self.fetch_mark_price("BTC/USDT")
            if (total_asset_btc or total_liab_btc)
            else Decimal("0")
        )
        balances: dict[str, Balance] = {}
        for asset, total in (raw.get("total") or {}).items():
            if total is None:
                continue
            balances[asset] = Balance(
                asset=asset,
                free=_d((raw.get("free") or {}).get(asset)),
                used=_d((raw.get("used") or {}).get(asset)),
                total=_d(total),
            )
        return MarginAccount(
            total_asset_value=total_asset_btc * btc_usdt,
            total_liability_value=total_liab_btc * btc_usdt,
            margin_level=margin_level if margin_level > 0 else Decimal("9999"),
            balances=balances,
        )


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
