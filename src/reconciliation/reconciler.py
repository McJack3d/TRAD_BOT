"""Reconciliation loop.

Every `interval_seconds`, compare internal DB state to Binance-reported
balances and positions. Tolerances live in config. Drift within
tolerance → warning; beyond → halt new orders and alert.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from src.adapters.exchange_base import ExchangeAdapter
from src.config import ReconciliationConfig
from src.logging_setup import log
from src.state.db import Database
from src.state.models import SystemStatusEnum

NotifyCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class ReconciliationResult:
    ok: bool
    drifts: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class Reconciler:
    def __init__(
        self,
        db: Database,
        exchange: ExchangeAdapter,
        cfg: ReconciliationConfig,
        on_notify: NotifyCallback | None = None,
        perp_to_spot: dict[str, str] | None = None,
    ):
        self.db = db
        self.exchange = exchange
        self.cfg = cfg
        self.on_notify = on_notify or _noop_notify
        # Maps Binance perp symbol → the DB position's spot symbol so the
        # diff function compares apples-to-apples regardless of venue naming.
        self.perp_to_spot = perp_to_spot or {}
        self._task: asyncio.Task | None = None
        # USDT total at the previous reconciliation; the spec bounds the
        # drift between consecutive runs rather than reconstructing equity.
        self._last_usdt_total: Decimal | None = None

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
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("reconciler.loop.error", error=str(e))
            await asyncio.sleep(self.cfg.interval_seconds)

    async def run_once(self) -> ReconciliationResult:
        db_positions = await self.db.open_positions()
        ex_positions = await self.exchange.fetch_positions()
        ex_balances = await self.exchange.fetch_balances()

        drifts = self.diff(db_positions, ex_positions, ex_balances)
        drifts += await self._check_balance_drift(ex_balances)
        ok = len(drifts) == 0

        if ok:
            await self.db.touch_reconciliation_ok()
            log.info("reconciler.ok", positions=len(db_positions))
        else:
            # Distinguish warn-level vs halt-level drift.
            halt_level = any("BEYOND_TOLERANCE" in d for d in drifts)
            if halt_level:
                log.error("reconciler.drift.halt", drifts=drifts)
                await self.db.set_status(
                    SystemStatusEnum.HALTED,
                    reason=f"reconciliation drift: {'; '.join(drifts)}",
                )
                await self.on_notify(
                    "RECONCILIATION DRIFT (HALT)", "\n".join(drifts)
                )
            else:
                log.warning("reconciler.drift.tolerated", drifts=drifts)
                await self.db.touch_reconciliation_ok()

        return ReconciliationResult(ok=ok, drifts=drifts)

    def diff(
        self,
        db_positions: list,
        ex_positions: list,
        ex_balances: dict,
    ) -> list[str]:
        """Pure diff function — exposed for unit tests."""
        drifts: list[str] = []

        db_perp_by_symbol = {p.symbol: p.perp_qty for p in db_positions if p.perp_qty != 0}
        ex_perp_by_symbol = {
            self.perp_to_spot.get(p.symbol, p.symbol): p.qty
            for p in ex_positions
            if p.leg == "perp"
        }

        all_symbols = set(db_perp_by_symbol) | set(ex_perp_by_symbol)
        for sym in all_symbols:
            # Compare signed quantities: a long/short flip of equal size is
            # maximal drift, not zero drift.
            db_qty = db_perp_by_symbol.get(sym, Decimal("0"))
            ex_qty = ex_perp_by_symbol.get(sym, Decimal("0"))
            denom = max(abs(db_qty), abs(ex_qty), Decimal("1e-12"))
            rel_diff = abs(db_qty - ex_qty) / denom
            if rel_diff > self.cfg.position_size_tolerance_pct:
                drifts.append(
                    f"BEYOND_TOLERANCE perp qty {sym}: db={db_qty} exchange={ex_qty} "
                    f"rel_diff={rel_diff}"
                )

        return drifts

    async def _check_balance_drift(self, ex_balances: dict) -> list[str]:
        """Bound the USDT change between consecutive reconciliations.

        We can't reconstruct expected USDT from the DB without full PnL
        accounting, so per the spec we bound the drift between runs
        instead. A drop beyond `balance_tolerance_usdt` with recent order
        activity is expected (fills, fees, funding) and only logged; the
        same drop with NO recent orders has no legitimate explanation
        (withdrawal, liquidation, external trading) and halts.
        """
        usdt_total = Decimal("0")
        for bal in ex_balances.values():
            if bal.asset == "USDT":
                usdt_total += bal.total

        prev, self._last_usdt_total = self._last_usdt_total, usdt_total
        if prev is None:
            return []
        drop = prev - usdt_total
        if drop <= self.cfg.balance_tolerance_usdt:
            return []
        recent_orders = await self.db.order_count_in_window(self.cfg.interval_seconds * 2)
        if recent_orders > 0:
            log.warning(
                "reconciler.balance_drop.with_activity",
                drop=str(drop),
                recent_orders=recent_orders,
            )
            return []
        return [
            f"BEYOND_TOLERANCE USDT balance dropped {drop} "
            f"(prev={prev} now={usdt_total}) with no order activity"
        ]


async def _noop_notify(_title: str, _body: str) -> None:
    return None
