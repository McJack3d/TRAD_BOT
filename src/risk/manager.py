"""Risk Manager.

Has unilateral authority to flatten positions and halt the bot. Runs:
- Pre-trade checks on every order (delegates to checks.py).
- Continuous monitoring loop every 10s.

If any continuous check fails, calls `flatten_and_halt(reason)` which
flips system status to HALTED and queues exit orders for every open
position. Operator must `/resume` after fixing root cause.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.adapters.exchange_base import ExchangeAdapter, ExchangePosition
from src.config import RiskConfig
from src.logging_setup import log
from src.risk.checks import PreTradeContext, run_pre_trade_checks
from src.state import Database
from src.state.models import SystemStatusEnum

FlattenCallback = Callable[[str], Awaitable[None]]
NotifyCallback = Callable[[str, str], Awaitable[None]]


class RiskManager:
    def __init__(
        self,
        db: Database,
        exchange: ExchangeAdapter,
        cfg: RiskConfig,
        starting_equity: Decimal,
        on_flatten: FlattenCallback | None = None,
        on_notify: NotifyCallback | None = None,
        monitor_interval_seconds: int = 10,
    ):
        self.db = db
        self.exchange = exchange
        self.cfg = cfg
        self.starting_equity = starting_equity
        self.on_flatten = on_flatten
        self.on_notify = on_notify or _noop_notify
        self.monitor_interval_seconds = monitor_interval_seconds
        self._task: asyncio.Task | None = None
        self._halted_reason: str | None = None

    # ---- pre-trade ---------------------------------------------------

    async def check_pre_trade(self, ctx: PreTradeContext) -> bool:
        """Return True if order may proceed; False (with logged reason) if not."""
        result = run_pre_trade_checks(ctx, self.cfg)
        if not result.ok:
            log.warning(
                "risk.pre_trade.reject",
                symbol=ctx.proposed_symbol,
                reason=result.reason,
                notional=str(ctx.proposed_notional),
            )
            await self.on_notify("Pre-trade rejected", f"{ctx.proposed_symbol}: {result.reason}")
            return False
        log.info(
            "risk.pre_trade.accept",
            symbol=ctx.proposed_symbol,
            notional=str(ctx.proposed_notional),
        )
        return True

    # ---- continuous --------------------------------------------------

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
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("risk.continuous.error", error=str(e))
            await asyncio.sleep(self.monitor_interval_seconds)

    async def tick(self) -> None:
        """One pass of continuous monitoring. Public for tests."""
        status = await self.db.get_status()
        if status.status == SystemStatusEnum.HALTED:
            return

        # Reconciliation freshness.
        if status.last_reconciliation_ok is None or (
            datetime.now(UTC) - status.last_reconciliation_ok
            > timedelta(seconds=self.cfg.reconciliation_stale_seconds)
        ):
            await self._halt("reconciliation stale")
            return

        # Snapshot of exchange-reported positions.
        positions = await self.exchange.fetch_positions()

        # Liquidation-distance + margin top-up checks for each short perp.
        for p in positions:
            if p.leg != "perp" or p.qty >= 0:
                continue
            liq_dist = _liq_distance_pct(p)
            if liq_dist is None:
                continue
            if liq_dist < self.cfg.liquidation_halt_pct:
                await self._halt(f"liq distance {liq_dist:.4f} < {self.cfg.liquidation_halt_pct}")
                return
            if liq_dist < self.cfg.margin_top_up_pct:
                top_up = (self.cfg.margin_top_up_pct - liq_dist) * p.margin
                log.warning(
                    "risk.margin.top_up",
                    symbol=p.symbol,
                    liq_distance=str(liq_dist),
                    amount=str(top_up),
                )
                try:
                    await self.exchange.add_margin(p.symbol, top_up)
                    await self.on_notify(
                        "Margin top-up",
                        f"{p.symbol}: liq dist {liq_dist:.4f}, added {top_up}",
                    )
                except Exception as e:
                    log.exception("risk.margin.top_up.failed", symbol=p.symbol, error=str(e))

        # Daily and cumulative loss stops.
        snap = await self.db.latest_snapshot()
        if snap is not None:
            daily_total = snap.realized_pnl_daily
            stop_daily = self.starting_equity * self.cfg.daily_loss_stop_pct
            if daily_total <= -stop_daily:
                await self._halt(f"daily loss stop hit: {daily_total} <= -{stop_daily}")
                return
            stop_cum = self.starting_equity * self.cfg.cumulative_loss_stop_pct
            if snap.realized_pnl_cumulative <= -stop_cum:
                await self._halt(
                    f"cumulative loss stop hit: {snap.realized_pnl_cumulative} <= -{stop_cum}"
                )
                return

    async def _halt(self, reason: str) -> None:
        if self._halted_reason == reason:
            return
        self._halted_reason = reason
        log.error("risk.halt", reason=reason)
        await self.db.set_status(SystemStatusEnum.HALTED, reason=reason)
        if self.on_flatten:
            try:
                await self.on_flatten(reason)
            except Exception as e:
                log.exception("risk.flatten.failed", error=str(e))
        await self.on_notify("RISK HALT", reason)


def _liq_distance_pct(p: ExchangePosition) -> Decimal | None:
    """Distance from mark to liquidation, expressed as a fraction of margin.

    For a short position: liq_price > entry_price > mark_price (ideally).
    If mark moves up to liq_price, the loss equals (liq_price - entry) * qty,
    which on isolated margin equals the deposited margin. We express
    "distance" as how much margin headroom remains as a fraction of the
    initial margin.
    """
    if p.liquidation_price is None or p.margin <= 0:
        return None
    if p.qty == 0:
        return None
    # Loss-if-mark-equals-current = (mark - entry) * qty (short qty is negative).
    current_loss = (p.mark_price - p.entry_price) * p.qty
    # Loss-if-mark-equals-liq = (liq - entry) * qty.
    loss_at_liq = (p.liquidation_price - p.entry_price) * p.qty
    if loss_at_liq == 0:
        return None
    remaining = (loss_at_liq - current_loss) / loss_at_liq
    if remaining < 0:
        return Decimal("0")
    return remaining


async def _noop_notify(_title: str, _body: str) -> None:
    return None
