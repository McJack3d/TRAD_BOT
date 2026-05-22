"""In-memory fake exchange adapter for paper mode and tests.

Implements the full `ExchangeAdapter` interface deterministically: orders
fill immediately at a configurable mid price plus slippage; positions
and balances are tracked in memory; funding rate is set by the harness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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


class FakeExchange(ExchangeAdapter):
    """Configurable, fill-on-submit fake exchange.

    Tests/paper-mode set prices and funding via `set_ticker`, `set_funding`,
    or by mutating the internal dicts. Every submitted order fills at the
    current mid price (or `last`) with `slippage_bps` deterministic skew.
    """

    def __init__(
        self,
        starting_usdt: Decimal = Decimal("1000"),
        slippage_bps: Decimal = Decimal("2.0"),
        fee_bps: Decimal = Decimal("4.0"),
    ):
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        # asset → Balance, keyed once per (leg, asset).
        self._balances: dict[str, Balance] = {
            "spot:USDT": Balance("USDT", starting_usdt / 2, Decimal("0"), starting_usdt / 2),
            "perp:USDT": Balance("USDT", starting_usdt / 2, Decimal("0"), starting_usdt / 2),
        }
        # symbol → Ticker (per leg)
        self._tickers: dict[tuple[str, Leg], Ticker] = {}
        self._funding: dict[str, FundingRate] = {}
        self._positions: dict[str, ExchangePosition] = {}  # symbol → perp position
        self._orders: dict[str, ExchangeOrder] = {}  # client_order_id → order
        self._server_time_ms = int(datetime.now(UTC).timestamp() * 1000)
        self._leverage: dict[str, int] = {}

    # ---- harness helpers ---------------------------------------------

    def set_ticker(self, symbol: str, leg: Leg, last: Decimal, spread_bps: Decimal = Decimal("5")) -> None:
        spread = last * spread_bps / Decimal("10000")
        self._tickers[(symbol, leg)] = Ticker(
            symbol=symbol,
            bid=last - spread / 2,
            ask=last + spread / 2,
            last=last,
            ts=datetime.now(UTC),
        )

    def set_funding(self, symbol: str, rate: Decimal, mark: Decimal) -> None:
        self._funding[symbol] = FundingRate(
            symbol=symbol,
            rate=rate,
            next_funding_time=datetime.now(UTC) + timedelta(hours=8),
            mark_price=mark,
        )

    def advance_clock(self, ms: int) -> None:
        self._server_time_ms += ms

    # ---- ExchangeAdapter ---------------------------------------------

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def fetch_balances(self) -> dict[str, Balance]:
        return dict(self._balances)

    async def fetch_positions(self) -> list[ExchangePosition]:
        return list(self._positions.values())

    async def fetch_server_time(self) -> int:
        return self._server_time_ms

    async def fetch_ticker(self, symbol: str, leg: Leg) -> Ticker:
        t = self._tickers.get((symbol, leg))
        if t is None:
            raise KeyError(f"no ticker set: {symbol} {leg}")
        return t

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        f = self._funding.get(symbol)
        if f is None:
            raise KeyError(f"no funding set: {symbol}")
        return f

    async def fetch_mark_price(self, symbol: str) -> Decimal:
        return (await self.fetch_funding_rate(symbol)).mark_price

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._leverage[symbol] = leverage

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
        ticker = self._tickers.get((symbol, leg))
        if ticker is None:
            raise KeyError(f"no ticker for {symbol} {leg}; cannot fill")
        mid = (ticker.bid + ticker.ask) / 2 if ticker.ask else ticker.last
        slip = mid * self.slippage_bps / Decimal("10000")
        fill_price = mid + slip if side == "buy" else mid - slip
        fee = qty * fill_price * self.fee_bps / Decimal("10000")

        if leg == "perp":
            self._apply_perp_fill(symbol, side, qty, fill_price, reduce_only)
        else:
            self._apply_spot_fill(symbol, side, qty, fill_price, fee)

        order = ExchangeOrder(
            client_order_id=client_order_id,
            exchange_order_id=f"fake-{client_order_id}",
            symbol=symbol,
            leg=leg,
            side=side,
            qty=qty,
            filled_qty=qty,
            avg_price=fill_price,
            status="filled",
            fee_paid=fee,
            fee_asset="USDT",
            ts=datetime.now(UTC),
        )
        self._orders[client_order_id] = order
        return order

    async def fetch_order(
        self, client_order_id: str, symbol: str, leg: Leg
    ) -> ExchangeOrder | None:
        return self._orders.get(client_order_id)

    async def cancel_order(self, client_order_id: str, symbol: str, leg: Leg) -> None:
        return None

    async def add_margin(self, symbol: str, amount: Decimal) -> None:
        pos = self._positions.get(symbol)
        if pos is None:
            return
        if ":" in symbol:
            margin_asset = symbol.split(":")[-1]
        else:
            margin_asset = symbol.split("/")[-1]
        self._positions[symbol] = ExchangePosition(
            symbol=pos.symbol,
            leg=pos.leg,
            qty=pos.qty,
            entry_price=pos.entry_price,
            mark_price=pos.mark_price,
            liquidation_price=pos.liquidation_price,
            margin=pos.margin + amount,
            unrealized_pnl=pos.unrealized_pnl,
        )
        self._debit("perp", margin_asset, amount)

    # ---- internals ---------------------------------------------------

    def _apply_spot_fill(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
    ) -> None:
        base, quote = symbol.split("/", maxsplit=1)
        if side == "buy":
            self._debit("spot", quote, qty * price + fee)
            self._credit("spot", base, qty)
        else:
            self._debit("spot", base, qty)
            self._credit("spot", quote, qty * price - fee)

    def _apply_perp_fill(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        reduce_only: bool,
    ) -> None:
        # Perp pairs on Binance use settle-currency notation like
        # "BTC/USDT:USDT" — the part after the colon is the margin asset.
        # Fall back to the quote currency if no colon present.
        if ":" in symbol:
            margin_asset = symbol.split(":")[-1]
        else:
            margin_asset = symbol.split("/")[-1]
        pos = self._positions.get(symbol)
        signed = qty if side == "buy" else -qty
        if pos is None:
            leverage = Decimal(self._leverage.get(symbol, 2))
            margin = qty * price / leverage
            self._debit("perp", margin_asset, margin)
            self._positions[symbol] = ExchangePosition(
                symbol=symbol,
                leg="perp",
                qty=signed,
                entry_price=price,
                mark_price=price,
                liquidation_price=_estimate_liq_price(signed, price, margin),
                margin=margin,
                unrealized_pnl=Decimal("0"),
            )
            return

        new_qty = pos.qty + signed
        if new_qty == 0:
            # Closed; release margin + realize PnL.
            pnl = (pos.entry_price - price) * pos.qty  # short qty negative
            self._credit("perp", margin_asset, pos.margin + pnl)
            del self._positions[symbol]
            return

        # Partial reduce or add.
        self._positions[symbol] = ExchangePosition(
            symbol=pos.symbol,
            leg="perp",
            qty=new_qty,
            entry_price=pos.entry_price,  # simplified: keep original entry
            mark_price=price,
            liquidation_price=_estimate_liq_price(new_qty, pos.entry_price, pos.margin),
            margin=pos.margin,
            unrealized_pnl=(pos.entry_price - price) * new_qty,
        )

    def _debit(self, leg: str, asset: str, amount: Decimal) -> None:
        key = f"{leg}:{asset}"
        bal = self._balances.get(key) or Balance(asset, Decimal("0"), Decimal("0"), Decimal("0"))
        new_total = bal.total - amount
        self._balances[key] = Balance(asset, new_total, Decimal("0"), new_total)

    def _credit(self, leg: str, asset: str, amount: Decimal) -> None:
        key = f"{leg}:{asset}"
        bal = self._balances.get(key) or Balance(asset, Decimal("0"), Decimal("0"), Decimal("0"))
        new_total = bal.total + amount
        self._balances[key] = Balance(asset, new_total, Decimal("0"), new_total)


def _estimate_liq_price(qty: Decimal, entry: Decimal, margin: Decimal) -> Decimal:
    """Very rough liquidation-price estimate for the fake.

    For a short (qty < 0), liq is where loss == margin:
        (liq - entry) * |qty| = margin  ⇒  liq = entry + margin / |qty|
    """
    if qty == 0:
        return Decimal("0")
    return entry + margin / abs(qty)
