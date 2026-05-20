"""Two-leg order coordination with idempotency and partial-fill handling.

Opening a pair = spot buy + perp short, ideally simultaneously. We sequence
them: spot first (the underlying), then perp short. If the perp leg fails
after the spot leg fills, we immediately try to unwind the spot to keep the
book delta-neutral. If unwind also fails, we halt and alert — a human must
unwind manually.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from src.adapters.exchange_base import ExchangeAdapter, ExchangeOrder, Leg, Side
from src.config import BotConfig, Mode, SymbolConfig
from src.execution.order import generate_client_order_id, round_qty
from src.logging_setup import log
from src.state import Database
from src.state.models import (
    Fill,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    SystemStatusEnum,
)

# Status strings that Binance returns. Normalize to our enum.
_STATUS_MAP = {
    "open": OrderStatus.NEW,
    "new": OrderStatus.NEW,
    "closed": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "partial": OrderStatus.PARTIALLY_FILLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def _normalize_status(s: str) -> OrderStatus:
    return _STATUS_MAP.get((s or "").lower(), OrderStatus.UNKNOWN)


class ExecutionEngine:
    def __init__(
        self,
        cfg: BotConfig,
        db: Database,
        exchange: ExchangeAdapter,
        dry_run: bool = False,
    ):
        self.cfg = cfg
        self.db = db
        self.exchange = exchange
        self.dry_run = dry_run or cfg.mode in (Mode.PAPER, Mode.DRY_RUN, Mode.BACKTEST)
        self._lock = asyncio.Lock()

    def _symbol_config(self, symbol: str) -> SymbolConfig:
        for s in self.cfg.symbols:
            if s.spot == symbol or s.perp == symbol:
                return s
        raise KeyError(f"symbol not configured: {symbol}")

    # ---- pair open ----------------------------------------------------

    async def open_pair(self, symbol: str, notional_usdt: Decimal) -> Position | None:
        """Open a delta-neutral pair: buy spot, short perp."""
        async with self._lock:
            sc = self._symbol_config(symbol)
            spot_price = await self._mid_price(sc.spot, "spot")
            if spot_price <= 0:
                log.error("execution.open_pair.no_price", symbol=symbol)
                return None

            qty = round_qty(notional_usdt / spot_price, sc.qty_step)
            if qty < sc.min_qty:
                log.warning(
                    "execution.open_pair.below_min", symbol=symbol, qty=str(qty), min=str(sc.min_qty)
                )
                return None

            # Ensure perp leverage / margin mode are set.
            if not self.dry_run:
                try:
                    await self.exchange.set_leverage(sc.perp, self.cfg.strategy.perp_leverage)
                except Exception as e:
                    log.warning("execution.set_leverage.failed", symbol=sc.perp, error=str(e))

            spot_order = await self._submit_with_idempotency(
                symbol=sc.spot, leg="spot", side="buy", qty=qty
            )
            if spot_order is None or spot_order.status not in (
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                log.error("execution.spot_leg.failed", symbol=sc.spot, status=str(spot_order))
                return None

            spot_filled = spot_order.filled_qty
            perp_order = await self._submit_with_idempotency(
                symbol=sc.perp, leg="perp", side="sell", qty=spot_filled
            )

            if perp_order is None or perp_order.status not in (
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                log.error("execution.perp_leg.failed_unwinding_spot", symbol=sc.perp)
                await self._emergency_unwind_spot(sc.spot, spot_filled)
                return None

            position = await self.db.create_position(
                Position(
                    symbol=symbol,
                    status=PositionStatus.OPEN,
                    spot_qty=spot_filled,
                    perp_qty=perp_order.filled_qty,
                    spot_entry_price=spot_order.avg_fill_price,
                    perp_entry_price=perp_order.avg_fill_price,
                    initial_margin=(perp_order.filled_qty * perp_order.avg_fill_price)
                    / Decimal(self.cfg.strategy.perp_leverage),
                    opened_at=datetime.now(UTC),
                )
            )
            log.info(
                "execution.open_pair.done",
                symbol=symbol,
                position_id=position.id,
                spot_qty=str(spot_filled),
                perp_qty=str(perp_order.filled_qty),
            )
            return position

    # ---- pair close ---------------------------------------------------

    async def close_pair(self, position_id: int, reason: str = "") -> None:
        async with self._lock:
            pos = await self.db.get_position(position_id)
            if pos is None or pos.status != PositionStatus.OPEN:
                return
            sc = self._symbol_config(pos.symbol)

            # Close perp first (reduce_only), then sell spot.
            perp_order = await self._submit_with_idempotency(
                symbol=sc.perp,
                leg="perp",
                side="buy",
                qty=abs(pos.perp_qty),
                reduce_only=True,
            )
            spot_order = await self._submit_with_idempotency(
                symbol=sc.spot, leg="spot", side="sell", qty=pos.spot_qty
            )

            spot_avg = spot_order.avg_fill_price if spot_order else Decimal("0")
            perp_avg = perp_order.avg_fill_price if perp_order else Decimal("0")
            spot_pnl = (spot_avg - pos.spot_entry_price) * pos.spot_qty
            perp_pnl = (pos.perp_entry_price - perp_avg) * abs(pos.perp_qty)
            realized = spot_pnl + perp_pnl + pos.funding_collected

            await self.db.close_position(position_id, realized_pnl=realized)
            log.info(
                "execution.close_pair.done",
                position_id=position_id,
                reason=reason,
                realized=str(realized),
            )

    # ---- emergency unwind --------------------------------------------

    async def emergency_flatten_all(self, reason: str) -> None:
        log.error("execution.emergency_flatten_all", reason=reason)
        positions = await self.db.open_positions()
        for p in positions:
            try:
                await self.close_pair(p.id, reason=f"emergency: {reason}")
            except Exception as e:
                log.exception("execution.emergency_flatten.error", position_id=p.id, error=str(e))

    async def _emergency_unwind_spot(self, symbol: str, qty: Decimal) -> None:
        if qty <= 0:
            return
        try:
            await self._submit_with_idempotency(symbol=symbol, leg="spot", side="sell", qty=qty)
            log.warning("execution.emergency_unwind_spot.done", symbol=symbol, qty=str(qty))
        except Exception as e:
            log.exception("execution.emergency_unwind_spot.failed", symbol=symbol, error=str(e))
            await self.db.set_status(
                SystemStatusEnum.HALTED,
                reason=f"emergency spot unwind failed for {symbol}: {e}",
            )

    # ---- submit + reconcile ------------------------------------------

    async def _submit_with_idempotency(
        self,
        symbol: str,
        leg: Leg,
        side: Side,
        qty: Decimal,
        reduce_only: bool = False,
    ) -> ExchangeOrder | None:
        """Submit an order, retrying once via `fetch_order` on timeout."""
        client_id = generate_client_order_id(prefix=f"{leg[0]}{side[0]}")
        order_row = await self.db.add_order(
            Order(
                client_order_id=client_id,
                symbol=symbol,
                leg=leg,
                side=side,
                qty=qty,
                status=OrderStatus.NEW,
            )
        )

        if self.dry_run:
            # Simulate immediate fill at last known mid.
            mid = await self._mid_price(symbol, leg)
            fake = ExchangeOrder(
                client_order_id=client_id,
                exchange_order_id=f"dry-{client_id}",
                symbol=symbol,
                leg=leg,
                side=side,
                qty=qty,
                filled_qty=qty,
                avg_price=mid,
                status="filled",
                fee_paid=qty * mid * (self.cfg.fees.spot_taker_bps / Decimal("10000")),
                fee_asset="USDT",
                ts=datetime.now(UTC),
            )
            await self._persist_order_outcome(order_row.id, client_id, fake)
            return fake

        try:
            result = await self.exchange.submit_order(
                symbol=symbol,
                leg=leg,
                side=side,
                qty=qty,
                client_order_id=client_id,
                reduce_only=reduce_only,
            )
        except Exception as e:
            log.warning("execution.submit.exception", symbol=symbol, leg=leg, error=str(e))
            # Idempotent retry: ask the exchange whether the order landed.
            await asyncio.sleep(1.0)
            result = await self.exchange.fetch_order(client_id, symbol, leg)
            if result is None:
                await self.db.update_order_status(client_id, OrderStatus.REJECTED)
                return None

        # Poll for terminal state if not yet filled.
        result = await self._wait_terminal(result, symbol, leg, client_id)
        await self._persist_order_outcome(order_row.id, client_id, result)
        return result

    async def _wait_terminal(
        self,
        result: ExchangeOrder,
        symbol: str,
        leg: Leg,
        client_id: str,
        max_wait_seconds: int = 10,
    ) -> ExchangeOrder:
        status = _normalize_status(result.status)
        if status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            return result
        deadline = asyncio.get_event_loop().time() + max_wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            try:
                refreshed = await self.exchange.fetch_order(client_id, symbol, leg)
                if refreshed is None:
                    continue
                result = refreshed
                if _normalize_status(refreshed.status) in (
                    OrderStatus.FILLED,
                    OrderStatus.CANCELED,
                    OrderStatus.REJECTED,
                    OrderStatus.EXPIRED,
                ):
                    return refreshed
            except Exception as e:
                log.warning("execution.fetch_order.error", symbol=symbol, error=str(e))
        return result

    async def _persist_order_outcome(
        self, order_id: int, client_id: str, result: ExchangeOrder
    ) -> None:
        status = _normalize_status(result.status)
        if result.filled_qty > 0 and status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            # Partial-fill safety: if some quantity filled but exchange marked closed for other reason.
            status = OrderStatus.PARTIALLY_FILLED if result.filled_qty < result.qty else OrderStatus.FILLED
        await self.db.update_order_status(
            client_order_id=client_id,
            status=status,
            filled_qty=result.filled_qty,
            avg_price=result.avg_price,
            exchange_order_id=result.exchange_order_id,
            fee_paid=result.fee_paid,
        )
        if result.filled_qty > 0:
            await self.db.add_fill(
                Fill(
                    order_id=order_id,
                    exchange_trade_id=result.exchange_order_id or client_id,
                    qty=result.filled_qty,
                    price=result.avg_price,
                    fee=result.fee_paid,
                    fee_asset=result.fee_asset,
                )
            )

    async def _mid_price(self, symbol: str, leg: Leg) -> Decimal:
        t = await self.exchange.fetch_ticker(symbol, leg)
        if t.bid > 0 and t.ask > 0:
            return (t.bid + t.ask) / 2
        return t.last
