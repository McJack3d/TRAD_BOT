"""SimpleBot: BTC trend-follower orchestrator.

Once per `tick()` call:
  1. Fetch the last N daily closes from the exchange.
  2. Compute the SMA signal.
  3. If signal differs from current position, flip.

Held state (persisted in DB):
  - `enabled`: whether trading is active.
  - `current_state`: IN (holding BTC) or OUT (holding USDT).
  - `last_evaluated`: timestamp of the most recent tick.
  - Trade history via `Order` / `Fill` rows.

This is the laptop-friendly counterpart to the funding-arb daemon.
Strategy evaluates on daily bars so missing an evaluation by hours
costs nothing — the next tick catches up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
from sqlalchemy import select, update

from src.adapters.exchange_base import ExchangeAdapter
from src.execution.spot_only import go_in, go_out
from src.logging_setup import log
from src.state.db import Database
from src.state.models import SystemStatus
from src.strategy.sma_trend import TrendSignal, TrendState, evaluate_trend


@dataclass
class BotStatus:
    enabled: bool
    current_state: TrendState
    last_signal: TrendSignal | None
    last_evaluated: datetime | None
    btc_qty: Decimal
    usdt_qty: Decimal
    last_price: Decimal


class SimpleBot:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        db: Database,
        symbol: str = "BTC/USDT",
        sma_window: int = 200,
    ):
        self.exchange = exchange
        self.db = db
        self.symbol = symbol
        self.sma_window = sma_window
        # `system_status.halt_reason` doubles as our state cache so we can
        # persist current TrendState across restarts without adding a schema.
        # Format: "trend:<state>|enabled:<true|false>|signal_reason:<...>"
        self._last_signal: TrendSignal | None = None
        self._last_evaluated: datetime | None = None

    # ---- enable / disable -------------------------------------------

    async def enable(self) -> None:
        await self._set_meta(enabled=True)
        log.info("simple_bot.enabled")

    async def disable(self) -> None:
        await self._set_meta(enabled=False)
        log.info("simple_bot.disabled")

    async def is_enabled(self) -> bool:
        meta = await self._get_meta()
        return meta.get("enabled") == "true"

    # ---- evaluation + trading ---------------------------------------

    async def evaluate(self) -> TrendSignal:
        closes = await self._fetch_daily_closes()
        signal = evaluate_trend(closes, self.sma_window)
        self._last_signal = signal
        self._last_evaluated = datetime.now(UTC)
        return signal

    async def tick(self) -> TrendSignal | None:
        """One evaluation cycle. If enabled and signal differs from holdings,
        rebalance. Returns the signal, or None if disabled / data missing."""
        if not await self.is_enabled():
            log.info("simple_bot.tick.disabled")
            return None
        signal = await self.evaluate()
        current = await self.current_state()
        if signal.state == current:
            log.info("simple_bot.tick.no_change", state=current.value)
            return signal
        if signal.state == TrendState.IN:
            await go_in(self.exchange, self.db, symbol=self.symbol)
        else:
            await go_out(self.exchange, self.db, symbol=self.symbol)
        await self._set_meta(current_state=signal.state.value)
        log.info(
            "simple_bot.tick.flipped",
            from_=current.value,
            to=signal.state.value,
            reason=signal.reason,
        )
        return signal

    async def flatten_now(self) -> None:
        """Force-sell BTC to USDT regardless of signal. Useful before going
        offline for a long stretch."""
        await go_out(self.exchange, self.db, symbol=self.symbol)
        await self._set_meta(current_state=TrendState.OUT.value)
        log.info("simple_bot.flatten")

    # ---- status -----------------------------------------------------

    async def status(self) -> BotStatus:
        enabled = await self.is_enabled()
        current = await self.current_state()
        balances = await self.exchange.fetch_balances()
        base = self.symbol.split("/", maxsplit=1)[0]
        btc = balances.get(f"spot:{base}")
        usdt = balances.get("spot:USDT")
        last_price = Decimal("0")
        try:
            t = await self.exchange.fetch_ticker(self.symbol, "spot")
            last_price = t.last
        except Exception as e:
            log.warning("simple_bot.status.no_price", error=str(e))
        return BotStatus(
            enabled=enabled,
            current_state=current,
            last_signal=self._last_signal,
            last_evaluated=self._last_evaluated,
            btc_qty=btc.total if btc else Decimal("0"),
            usdt_qty=usdt.total if usdt else Decimal("0"),
            last_price=last_price,
        )

    async def current_state(self) -> TrendState:
        meta = await self._get_meta()
        return TrendState(meta.get("current_state", "out"))

    # ---- meta persistence -------------------------------------------

    async def _get_meta(self) -> dict[str, str]:
        async with self.db.session() as s:
            row = (await s.execute(select(SystemStatus).where(SystemStatus.id == 1))).scalar_one_or_none()
            raw = row.halt_reason if row else None
        if not raw:
            return {}
        out = {}
        for chunk in raw.split("|"):
            if ":" in chunk:
                k, v = chunk.split(":", 1)
                out[k] = v
        return out

    async def _set_meta(self, **kwargs: str) -> None:
        meta = await self._get_meta()
        meta.update({k: str(v).lower() for k, v in kwargs.items()})
        encoded = "|".join(f"{k}:{v}" for k, v in meta.items())
        async with self.db.session() as s:
            await s.execute(
                update(SystemStatus).where(SystemStatus.id == 1).values(halt_reason=encoded)
            )
            await s.commit()

    # ---- data -------------------------------------------------------

    async def _fetch_daily_closes(self) -> pd.Series:
        """Fetch the last N+5 daily closes via the exchange's CCXT clients.

        FakeExchange doesn't expose OHLCV; in paper mode the caller injects
        synthetic closes via the `closes_override` attribute on the bot.
        """
        override = getattr(self, "closes_override", None)
        if override is not None:
            return pd.Series(override)
        ccxt_spot = getattr(self.exchange, "spot", None)
        if ccxt_spot is None:
            raise RuntimeError(
                "exchange has no .spot ccxt client and no closes_override is set"
            )
        ohlcv = await ccxt_spot.fetch_ohlcv(self.symbol, "1d", limit=self.sma_window + 5)
        closes = [float(c[4]) for c in ohlcv]
        return pd.Series(closes)
