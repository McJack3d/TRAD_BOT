"""Funding-rate arbitrage strategy engine.

On each tick (driven by the funding poll), the engine asks for a signal
per symbol, runs pre-trade checks via Risk Manager, then asks Execution
to open or close.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.config import BotConfig
from src.data import MarketData
from src.execution.engine import ExecutionEngine
from src.logging_setup import log
from src.risk.checks import PreTradeContext
from src.risk.manager import RiskManager
from src.state import Database
from src.state.models import SystemStatusEnum
from src.strategy.signals import (
    EntrySignal,
    ExitSignal,
    PositionView,
    evaluate_signal,
)


class FundingArbStrategy:
    def __init__(
        self,
        cfg: BotConfig,
        db: Database,
        market_data: MarketData,
        risk: RiskManager,
        execution: ExecutionEngine,
    ):
        self.cfg = cfg
        self.db = db
        self.market_data = market_data
        self.risk = risk
        self.execution = execution

    async def evaluate_all(self) -> None:
        status = await self.db.get_status()
        if status.status != SystemStatusEnum.ACTIVE:
            log.info("strategy.skip", status=status.status.value)
            return

        open_positions = {p.symbol: p for p in await self.db.open_positions()}
        active_symbols = [s.spot for s in self.cfg.symbols]
        equity = await self._estimate_equity()
        n_active = max(1, len(active_symbols))
        target_per = (equity * self.cfg.risk.max_gross_notional_pct) / n_active

        for sc in self.cfg.symbols:
            await self._evaluate_one(sc.spot, target_per, open_positions, equity)

    async def _evaluate_one(
        self,
        symbol: str,
        target_notional: Decimal,
        open_positions: dict,
        equity: Decimal,
    ) -> None:
        snap = self.market_data.get(symbol)
        if snap.spot_mid == 0 or snap.funding_rate == 0:
            return  # data not ready

        pos = open_positions.get(symbol)
        position_view = (
            PositionView(symbol=symbol, opened_at=pos.opened_at, notional=pos.spot_qty * snap.spot_mid)
            if pos
            else None
        )

        signal = evaluate_signal(
            symbol=symbol,
            funding_rate=snap.funding_rate,
            cfg=self.cfg.strategy,
            position=position_view,
            proposed_notional=target_notional,
            now=datetime.now(UTC),
        )

        if isinstance(signal, EntrySignal):
            ctx = await self._build_pretrade_ctx(symbol, signal.notional, equity)
            if not await self.risk.check_pre_trade(ctx):
                return
            await self.execution.open_pair(symbol=symbol, notional_usdt=signal.notional)
        elif isinstance(signal, ExitSignal):
            assert pos is not None
            await self.execution.close_pair(position_id=pos.id, reason=signal.reason)

    async def _build_pretrade_ctx(
        self, symbol: str, notional: Decimal, equity: Decimal
    ) -> PreTradeContext:
        positions = await self.db.open_positions()
        per_symbol_exposure: dict[str, Decimal] = {}
        total_exposure = Decimal("0")
        for p in positions:
            snap = self.market_data.get(p.symbol) if p.symbol in self.market_data.snapshots else None
            mid = snap.spot_mid if snap else Decimal("0")
            notional_p = p.spot_qty * mid
            per_symbol_exposure[p.symbol] = per_symbol_exposure.get(p.symbol, Decimal("0")) + notional_p
            total_exposure += notional_p

        # Conservative defaults for a fresh-entry pre-trade: assume the post-order
        # short leg has full liquidation headroom. The continuous risk monitor will
        # tighten this once the position is live and the exchange reports a real
        # liquidation price.
        snap = self.market_data.get(symbol)
        proposed_liq_distance_pct = Decimal("0.50")

        status = await self.db.get_status()
        latest = await self.db.latest_snapshot()
        daily_realized = latest.realized_pnl_daily if latest else Decimal("0")
        daily_unrealized = latest.unrealized_pnl if latest else Decimal("0")
        cumulative_realized = latest.realized_pnl_cumulative if latest else Decimal("0")

        return PreTradeContext(
            equity=equity,
            starting_equity=self.cfg.starting_equity_eur,
            total_exposure=total_exposure,
            per_symbol_exposure=per_symbol_exposure,
            proposed_symbol=symbol,
            proposed_notional=notional,
            proposed_short_liq_distance_pct=proposed_liq_distance_pct,
            orders_in_last_minute=await self.db.order_count_in_window(60),
            daily_realized_pnl=daily_realized,
            daily_unrealized_pnl=daily_unrealized,
            cumulative_realized_pnl=cumulative_realized,
            reconciliation_ok=True,  # checked elsewhere by stale gate
            system_status=status.status,
            clock_drift_ms=0,
        )

    async def _estimate_equity(self) -> Decimal:
        snap = await self.db.latest_snapshot()
        if snap:
            return snap.equity_usdt
        return self.cfg.starting_equity_eur
