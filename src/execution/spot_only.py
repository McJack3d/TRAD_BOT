"""Single-leg spot executor for the trend bot.

Two operations: convert all USDT to BTC (`go_in`), or convert all BTC
to USDT (`go_out`). Each places a single market order via the same
`ExchangeAdapter` interface the funding-arb bot uses, so swapping the
backing exchange (`FakeExchange` ↔ Binance) is one line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.adapters.exchange_base import ExchangeAdapter, ExchangeOrder
from src.execution.order import generate_client_order_id, round_qty
from src.logging_setup import log
from src.state import Database
from src.state.models import Fill, Leg, Order, OrderStatus, Side


# Keep a small buffer so the order never tries to spend more than we have
# after exchange-side rounding and fee debit.
_BUFFER_PCT = Decimal("0.005")


async def go_in(
    exchange: ExchangeAdapter,
    db: Database,
    symbol: str = "BTC/USDT",
    qty_step: Decimal = Decimal("0.00001"),
    min_qty: Decimal = Decimal("0.00001"),
) -> ExchangeOrder | None:
    """Buy as much BTC as available USDT allows. Returns the fill, or None."""
    balances = await exchange.fetch_balances()
    usdt = balances.get("spot:USDT")
    if usdt is None or usdt.free <= 0:
        log.info("trend.go_in.no_usdt")
        return None

    ticker = await exchange.fetch_ticker(symbol, "spot")
    ask = ticker.ask or ticker.last
    if ask <= 0:
        log.warning("trend.go_in.no_price", symbol=symbol)
        return None

    budget = usdt.free * (Decimal("1") - _BUFFER_PCT)
    qty = round_qty(budget / ask, qty_step)
    if qty < min_qty:
        log.info("trend.go_in.below_min", qty=str(qty), min=str(min_qty))
        return None

    return await _submit_and_record(exchange, db, symbol, Side.BUY, qty)


async def go_out(
    exchange: ExchangeAdapter,
    db: Database,
    symbol: str = "BTC/USDT",
    qty_step: Decimal = Decimal("0.00001"),
    min_qty: Decimal = Decimal("0.00001"),
) -> ExchangeOrder | None:
    """Sell all BTC for USDT. Returns the fill, or None."""
    balances = await exchange.fetch_balances()
    base = symbol.split("/", maxsplit=1)[0]
    bal = balances.get(f"spot:{base}")
    if bal is None or bal.free <= 0:
        log.info("trend.go_out.no_base", asset=base)
        return None

    qty = round_qty(bal.free, qty_step)
    if qty < min_qty:
        log.info("trend.go_out.below_min", qty=str(qty), min=str(min_qty))
        return None

    return await _submit_and_record(exchange, db, symbol, Side.SELL, qty)


async def _submit_and_record(
    exchange: ExchangeAdapter,
    db: Database,
    symbol: str,
    side: Side,
    qty: Decimal,
) -> ExchangeOrder | None:
    client_id = generate_client_order_id(prefix=f"t{side.value[0]}")
    order_row = await db.add_order(
        Order(
            client_order_id=client_id,
            symbol=symbol,
            leg=Leg.SPOT,
            side=side,
            qty=qty,
            status=OrderStatus.NEW,
        )
    )
    try:
        result = await exchange.submit_order(
            symbol=symbol,
            leg="spot",
            side=side.value,
            qty=qty,
            client_order_id=client_id,
        )
    except Exception as e:
        log.exception("trend.submit.failed", symbol=symbol, side=side.value, error=str(e))
        await db.update_order_status(client_id, OrderStatus.REJECTED)
        return None

    await db.update_order_status(
        client_order_id=client_id,
        status=OrderStatus.FILLED if result.filled_qty >= qty else OrderStatus.PARTIALLY_FILLED,
        filled_qty=result.filled_qty,
        avg_price=result.avg_price,
        exchange_order_id=result.exchange_order_id,
        fee_paid=result.fee_paid,
    )
    if result.filled_qty > 0:
        await db.add_fill(
            Fill(
                order_id=order_row.id,
                exchange_trade_id=result.exchange_order_id or client_id,
                qty=result.filled_qty,
                price=result.avg_price,
                fee=result.fee_paid,
                fee_asset=result.fee_asset,
                ts=datetime.now(UTC),
            )
        )
    log.info(
        "trend.fill",
        symbol=symbol,
        side=side.value,
        qty=str(result.filled_qty),
        price=str(result.avg_price),
    )
    return result
