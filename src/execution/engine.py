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
from src.state.db import Database
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


class OrderStateUnknown(RuntimeError):
    """Raised when an order was submitted but its outcome could not be
    determined (submit raised, and recovery fetches also failed). The
    system is halted before this is raised — a human must reconcile
    against the exchange before trading resumes."""


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
        # DRY_RUN and BACKTEST modes simulate fills inside the engine without
        # ever touching the exchange. PAPER mode goes through the exchange
        # adapter (typically `FakeExchange`) so the full pipeline is exercised.
        self.dry_run = dry_run or cfg.mode in (Mode.DRY_RUN, Mode.BACKTEST)
        self._lock = asyncio.Lock()

    def _symbol_config(self, symbol: str) -> SymbolConfig:
        for s in self.cfg.symbols:
            if symbol in (s.spot, s.perp):
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

            try:
                spot_order = await self._submit_with_idempotency(
                    symbol=sc.spot, leg="spot", side="buy", qty=qty
                )
            except OrderStateUnknown:
                # Nothing else is in flight; system already halted.
                return None
            if spot_order is None or spot_order.status not in (
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                log.error("execution.spot_leg.failed", symbol=sc.spot, status=str(spot_order))
                return None

            spot_filled = spot_order.filled_qty
            try:
                perp_order = await self._submit_with_idempotency(
                    symbol=sc.perp, leg="perp", side="sell", qty=spot_filled
                )
            except OrderStateUnknown:
                # The perp short may or may not exist on the exchange. Auto-
                # unwinding the spot here could leave a naked short, so hold
                # both legs as-is; the system is already halted and the
                # reconciler/operator must resolve the true state.
                log.error(
                    "execution.perp_leg.state_unknown.holding_spot",
                    symbol=sc.perp,
                    spot_qty=str(spot_filled),
                )
                return None

            if perp_order is None or perp_order.status not in (
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
            ):
                log.error("execution.perp_leg.failed_unwinding_spot", symbol=sc.perp)
                await self._emergency_unwind_spot(sc.spot, spot_filled)
                return None

            # If the perp hedge filled smaller than the spot bought, the
            # surplus spot is unhedged — sell it back so the recorded
            # position is delta-neutral. Sub-step residue is dust.
            surplus = round_qty(spot_filled - perp_order.filled_qty, sc.qty_step)
            if surplus >= sc.qty_step:
                log.warning(
                    "execution.open_pair.partial_hedge_trimming_spot",
                    symbol=symbol,
                    spot_filled=str(spot_filled),
                    perp_filled=str(perp_order.filled_qty),
                    surplus=str(surplus),
                )
                await self._emergency_unwind_spot(sc.spot, surplus)
                spot_filled -= surplus

            position = await self.db.create_position(
                Position(
                    symbol=symbol,
                    status=PositionStatus.OPEN,
                    spot_qty=spot_filled,
                    perp_qty=-perp_order.filled_qty,  # short → signed negative
                    spot_entry_price=spot_order.avg_price,
                    perp_entry_price=perp_order.avg_price,
                    initial_margin=(perp_order.filled_qty * perp_order.avg_price)
                    / Decimal(self.cfg.strategy.perp_leverage),
                    opened_at=datetime.now(UTC),
                )
            )
            await self.db.assign_orders_to_position(
                [spot_order.client_order_id, perp_order.client_order_id], position.id
            )
            log.info(
                "execution.open_pair.done",
                symbol=symbol,
                position_id=position.id,
                spot_qty=str(spot_filled),
                perp_qty=str(-perp_order.filled_qty),
            )
            return position

    # ---- pair close ---------------------------------------------------

    async def close_pair(self, position_id: int, reason: str = "") -> None:
        async with self._lock:
            pos = await self.db.get_position(position_id)
            if pos is None or pos.status != PositionStatus.OPEN:
                return
            sc = self._symbol_config(pos.symbol)

            # Close perp first (reduce_only), then sell spot. If the perp
            # close fails, do NOT sell the spot — that would turn a hedged
            # book into a naked short. Halt and leave the position OPEN.
            try:
                perp_order = await self._submit_with_idempotency(
                    symbol=sc.perp,
                    leg="perp",
                    side="buy",
                    qty=abs(pos.perp_qty),
                    reduce_only=True,
                )
            except OrderStateUnknown:
                log.error("execution.close_pair.perp_state_unknown", position_id=position_id)
                return
            if perp_order is None or perp_order.filled_qty <= 0:
                log.error("execution.close_pair.perp_leg_failed", position_id=position_id)
                await self.db.set_status(
                    SystemStatusEnum.HALTED,
                    reason=f"close_pair perp leg failed for position {position_id}",
                )
                return

            try:
                spot_order = await self._submit_with_idempotency(
                    symbol=sc.spot, leg="spot", side="sell", qty=pos.spot_qty
                )
            except OrderStateUnknown:
                log.error("execution.close_pair.spot_state_unknown", position_id=position_id)
                return
            if spot_order is None or spot_order.filled_qty <= 0:
                # Perp is closed but the spot sale failed: the book is now
                # long spot, unhedged. Halt and leave OPEN for the operator;
                # marking it closed would record garbage PnL (price 0).
                log.error("execution.close_pair.spot_leg_failed", position_id=position_id)
                await self.db.set_status(
                    SystemStatusEnum.HALTED,
                    reason=f"close_pair spot leg failed for position {position_id} "
                    "(perp already closed — book is long spot, unhedged)",
                )
                return

            await self.db.assign_orders_to_position(
                [perp_order.client_order_id, spot_order.client_order_id], position_id
            )
            spot_pnl = (spot_order.avg_price - pos.spot_entry_price) * pos.spot_qty
            perp_pnl = (pos.perp_entry_price - perp_order.avg_price) * abs(pos.perp_qty)
            # Fees: sum USDT-denominated fees across all fills linked to this
            # position (entry + exit legs). Fees charged in other assets
            # (e.g. BNB discounts) are excluded — they don't reduce USDT PnL
            # directly and converting them is out of scope here.
            fees = await self.db.position_fees(position_id, fee_asset="USDT")
            realized = spot_pnl + perp_pnl + pos.funding_collected - fees

            await self.db.close_position(position_id, realized_pnl=realized)
            log.info(
                "execution.close_pair.done",
                position_id=position_id,
                reason=reason,
                realized=str(realized),
                fees=str(fees),
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
            fee_bps = (
                self.cfg.fees.perp_taker_bps if leg == "perp" else self.cfg.fees.spot_taker_bps
            )
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
                fee_paid=qty * mid * (fee_bps / Decimal("10000")),
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
            # Idempotent recovery: ask the exchange whether the order landed.
            # The fetch itself can fail (the submit error was likely network);
            # retry with backoff before declaring the outcome unknown.
            result = await self._recover_order_state(client_id, symbol, leg)
            if result is None:
                await self.db.update_order_status(client_id, OrderStatus.REJECTED)
                return None

        # Poll for terminal state if not yet filled.
        result = await self._wait_terminal(result, symbol, leg, client_id)
        await self._persist_order_outcome(order_row.id, client_id, result)
        return result

    async def _recover_order_state(
        self, client_id: str, symbol: str, leg: Leg, attempts: int = 3
    ) -> ExchangeOrder | None:
        """After a failed submit, determine whether the order landed.

        Returns the exchange's view of the order, or None if the exchange
        confirms it never landed. If the exchange cannot be reached at all,
        halts the system and raises OrderStateUnknown — re-submitting blind
        could double an order that actually went through.
        """
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            await asyncio.sleep(float(attempt))
            try:
                return await self.exchange.fetch_order(client_id, symbol, leg)
            except Exception as e:
                last_error = e
                log.warning(
                    "execution.recover_order.fetch_failed",
                    symbol=symbol,
                    attempt=attempt,
                    error=str(e),
                )
        await self.db.update_order_status(client_id, OrderStatus.UNKNOWN)
        await self.db.set_status(
            SystemStatusEnum.HALTED,
            reason=f"order state unknown for {client_id} ({symbol}): {last_error}",
        )
        raise OrderStateUnknown(f"{client_id} ({symbol} {leg}): {last_error}")

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

        # Still not terminal after the deadline. Cancel so the order can't
        # rest on the book untracked, then take the exchange's final word
        # (the cancel may race a fill — the last fetch decides).
        log.warning("execution.wait_terminal.timeout_cancelling", symbol=symbol, client_id=client_id)
        try:
            await self.exchange.cancel_order(client_id, symbol, leg)
        except Exception as e:
            log.warning("execution.cancel_order.error", symbol=symbol, error=str(e))
        try:
            refreshed = await self.exchange.fetch_order(client_id, symbol, leg)
            if refreshed is not None:
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
