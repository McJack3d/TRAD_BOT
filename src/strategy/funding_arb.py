"""Funding-rate arbitrage strategy engine.

On each tick (driven by the funding poll), the engine asks for a signal
per symbol, runs pre-trade checks via Risk Manager, then asks Execution
to open or close.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from src.adapters.exchange_base import ExchangeAdapter
from src.config import BotConfig
from src.execution.engine import ExecutionEngine
from src.logging_setup import log
from src.risk.checks import PreTradeContext
from src.risk.manager import RiskManager
from src.state.db import Database
from src.state.models import SystemStatusEnum
from src.state.pnl import ensure_utc
from src.strategy.signals import (
    EntrySignal,
    ExitSignal,
    PositionView,
    evaluate_signal,
)

if TYPE_CHECKING:
    # Only needed for type hints. Imported lazily so this module loads
    # even when the live market-data package isn't present (e.g. unit
    # tests that drive the strategy with a stub market-data object).
    from src.data.market_data import MarketData


class FundingArbStrategy:
    def __init__(
        self,
        cfg: BotConfig,
        db: Database,
        market_data: MarketData,
        risk: RiskManager,
        execution: ExecutionEngine,
        exchange: ExchangeAdapter | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.market_data = market_data
        self.risk = risk
        self.execution = execution
        # Optional — only used for the clock-drift pre-trade check. When
        # absent (e.g. some tests) drift is reported as 0.
        self.exchange = exchange

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

        # Post-order liquidation headroom for a fresh isolated short.
        # At leverage L the perp leg can absorb roughly a (1/L) adverse
        # price move before liquidation, so we express the headroom as
        # 1/L. With the default leverage 2 this is 0.50 — the value that
        # used to be hardcoded — but now it actually tightens as leverage
        # rises (L=4 → 0.25, which the 0.30 pre-trade floor rejects).
        leverage = max(1, self.cfg.strategy.perp_leverage)
        proposed_liq_distance_pct = Decimal("1") / Decimal(leverage)

        status = await self.db.get_status()
        latest = await self.db.latest_snapshot()
        daily_realized = latest.realized_pnl_daily if latest else Decimal("0")
        daily_unrealized = latest.unrealized_pnl if latest else Decimal("0")
        cumulative_realized = latest.realized_pnl_cumulative if latest else Decimal("0")

        # Reconciliation freshness — mirror the continuous monitor's gate
        # so we never open into a stale-state window. The stored timestamp
        # comes back tz-naive from SQLite; normalize before comparing.
        last_recon = ensure_utc(status.last_reconciliation_ok)
        recon_ok = last_recon is not None and (
            datetime.now(UTC) - last_recon
            <= timedelta(seconds=self.cfg.risk.reconciliation_stale_seconds)
        )

        return PreTradeContext(
            equity=equity,
            starting_equity=self.cfg.starting_equity_usdt,
            total_exposure=total_exposure,
            per_symbol_exposure=per_symbol_exposure,
            proposed_symbol=symbol,
            proposed_notional=notional,
            proposed_short_liq_distance_pct=proposed_liq_distance_pct,
            orders_in_last_minute=await self.db.order_count_in_window(60),
            daily_realized_pnl=daily_realized,
            daily_unrealized_pnl=daily_unrealized,
            cumulative_realized_pnl=cumulative_realized,
            reconciliation_ok=recon_ok,
            system_status=status.status,
            clock_drift_ms=await self._measure_clock_drift(),
        )

    async def _measure_clock_drift(self) -> int:
        """Local-vs-exchange clock drift in ms. Returns 0 on fetch
        failure (so a transient REST hiccup never blocks trading), but
        logs it — a persistent failure shows up in the logs."""
        if self.exchange is None:
            return 0
        try:
            server_ms = await self.exchange.fetch_server_time()
        except Exception as e:  # noqa: BLE001
            log.warning("strategy.clock_drift.fetch_failed", error=str(e))
            return 0
        local_ms = int(datetime.now(UTC).timestamp() * 1000)
        return abs(local_ms - server_ms)

    async def _estimate_equity(self) -> Decimal:
        snap = await self.db.latest_snapshot()
        if snap:
            return snap.equity_usdt
        return self.cfg.starting_equity_usdt
