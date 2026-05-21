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
    btc_qty: Decimal  # quantity of the BASE asset (BTC, ETH, ...)
    usdt_qty: Decimal  # quantity of the QUOTE asset (USDT, USDC, EUR, ...)
    last_price: Decimal
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    last_price: Decimal


class SimpleBot:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        db: Database,
        symbol: str = "BTC/USDT",
        sma_window: int = 200,
        entry_buffer_pct: float = 0.01,
        exit_buffer_pct: float = 0.01,
        trailing_stop_pct: float = 0.0,
    ):
        self.exchange = exchange
        self.db = db
        self.symbol = symbol
        self.sma_window = sma_window
        self.entry_buffer_pct = entry_buffer_pct
        self.exit_buffer_pct = exit_buffer_pct
        self.trailing_stop_pct = trailing_stop_pct
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
        signal = evaluate_trend(
            closes,
            sma_window=self.sma_window,
            entry_buffer_pct=self.entry_buffer_pct,
            exit_buffer_pct=self.exit_buffer_pct,
        )
        self._last_signal = signal
        self._last_evaluated = datetime.now(UTC)
        return signal

    async def tick(self) -> TrendSignal | None:
        """One evaluation cycle. If enabled, evaluate the signal, check the
        trailing-stop guard, and rebalance if needed.

        Order of operations matters:
          1. Trailing stop first — if we're IN and current price is too far
             below the post-entry peak, force-exit regardless of the SMA
             signal. Sets a one-tick cooldown so we don't immediately re-buy.
          2. Then evaluate the SMA signal. If signal differs from current
             position (and we're not in cooldown), rebalance.
        """
        if not await self.is_enabled():
            log.info("simple_bot.tick.disabled")
            return None

        current = await self.current_state()
        meta = await self._get_meta()
        peak = Decimal(meta.get("peak_since_entry", "0") or "0")
        cooldown = meta.get("stop_cooldown") == "true"

        # Get fresh price for stop check + signal eval.
        try:
            ticker = await self.exchange.fetch_ticker(self.symbol, "spot")
            current_price = ticker.last or (ticker.bid + ticker.ask) / 2
        except Exception as e:
            log.warning("simple_bot.tick.no_price", error=str(e))
            current_price = Decimal("0")

        # Trailing stop check.
        if (
            current == TrendState.IN
            and self.trailing_stop_pct > 0
            and current_price > 0
            and peak > 0
        ):
            new_peak = max(peak, current_price)
            if new_peak != peak:
                await self._set_meta(peak_since_entry=str(new_peak))
                peak = new_peak
            trigger = peak * (Decimal("1") - Decimal(str(self.trailing_stop_pct)))
            if current_price <= trigger:
                log.warning(
                    "simple_bot.tick.trailing_stop",
                    peak=str(peak),
                    current=str(current_price),
                    trigger=str(trigger),
                )
                await go_out(self.exchange, self.db, symbol=self.symbol)
                await self._set_meta(
                    current_state=TrendState.OUT.value,
                    peak_since_entry="0",
                    stop_cooldown="true",
                )
                # Synthesize a "stop" signal for reporting.
                from src.strategy.sma_trend import TrendSignal as _TS
                return _TS(
                    state=TrendState.OUT,
                    close=current_price,
                    sma=Decimal("0"),
                    reason=f"trailing stop hit: peak {peak} → current {current_price}",
                )

        signal = await self.evaluate()

        # Clear cooldown once signal goes OUT (or stays OUT) — we only re-enter
        # after a fresh fully-fledged IN signal.
        if cooldown and signal.state == TrendState.OUT:
            await self._set_meta(stop_cooldown="false")
            cooldown = False

        if signal.state == current:
            log.info("simple_bot.tick.no_change", state=current.value)
            return signal
        if cooldown:
            log.info("simple_bot.tick.in_cooldown")
            return signal

        if signal.state == TrendState.IN:
            await go_in(self.exchange, self.db, symbol=self.symbol)
            # Track entry peak for trailing stop.
            entry_price = current_price if current_price > 0 else signal.close
            await self._set_meta(
                current_state=TrendState.IN.value,
                peak_since_entry=str(entry_price),
                stop_cooldown="false",
            )
        else:
            await go_out(self.exchange, self.db, symbol=self.symbol)
            await self._set_meta(
                current_state=TrendState.OUT.value, peak_since_entry="0"
            )
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
        base, quote = self.symbol.split("/", maxsplit=1)
        base_bal = balances.get(f"spot:{base}")
        quote_bal = balances.get(f"spot:{quote}")
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
            btc_qty=base_bal.total if base_bal else Decimal("0"),
            usdt_qty=quote_bal.total if quote_bal else Decimal("0"),
            last_price=last_price,
            base_asset=base,
            quote_asset=quote,
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
        # Retry with exponential backoff on transient Binance unavailability
        # (HTTP 5xx, network blip, Cloudflare hiccup). Hard-fail after 3 tries.
        import asyncio as _asyncio

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                ohlcv = await ccxt_spot.fetch_ohlcv(
                    self.symbol, "1d", limit=self.sma_window + 5
                )
                closes = [float(c[4]) for c in ohlcv]
                return pd.Series(closes)
            except Exception as e:
                last_exc = e
                log.warning(
                    "simple_bot.fetch_ohlcv.retry",
                    attempt=attempt + 1,
                    symbol=self.symbol,
                    error=str(e),
                )
                await _asyncio.sleep(1.5**attempt)
        assert last_exc is not None
        raise last_exc
