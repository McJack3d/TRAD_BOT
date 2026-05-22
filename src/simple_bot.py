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
        notifier=None,
        sentiment_source=None,
        sentiment_weight: float = 0.0,
    ):
        from src.notify import noop_notifier

        self.notifier = notifier or noop_notifier
        self.exchange = exchange
        self.db = db
        self.symbol = symbol
        self.sma_window = sma_window
        self.entry_buffer_pct = entry_buffer_pct
        self.exit_buffer_pct = exit_buffer_pct
        self.trailing_stop_pct = trailing_stop_pct
        # Optional sentiment source. When set (and sentiment_weight > 0),
        # each evaluation tilts the SMA thresholds by the current factor.
        self.sentiment_source = sentiment_source
        self.sentiment_weight = sentiment_weight
        self._last_sentiment = None  # last SentimentReading, for status display
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

    async def _current_sentiment(self) -> float | None:
        """Fetch the current sentiment factor, or None if no source is
        configured or the fetch fails. A failed fetch must never block a
        trade — we just fall back to the plain SMA signal."""
        if self.sentiment_source is None or self.sentiment_weight <= 0:
            return None
        try:
            reading = await self.sentiment_source.current()
        except Exception as e:
            log.warning("simple_bot.sentiment.fetch_failed", error=str(e))
            return None
        self._last_sentiment = reading
        return reading.value

    async def evaluate(self) -> TrendSignal:
        closes = await self._fetch_daily_closes()
        sentiment = await self._current_sentiment()
        signal = evaluate_trend(
            closes,
            sma_window=self.sma_window,
            entry_buffer_pct=self.entry_buffer_pct,
            exit_buffer_pct=self.exit_buffer_pct,
            sentiment=sentiment,
            sentiment_weight=self.sentiment_weight,
        )
        self._last_signal = signal
        self._last_evaluated = datetime.now(UTC)
        return signal

    async def tick(self) -> TrendSignal | None:
        """One evaluation cycle. If enabled, evaluate the signal, check the
        trailing-stop guard, and rebalance if needed.

        Order of operations:
          1. Read state + cooldown flag, then clear the cooldown immediately
             (it's a SAME-TICK guard from the previous run, not multi-tick).
          2. Trailing stop check — if we're IN and current price is too far
             below the post-entry peak, force-exit and set cooldown so we
             don't re-buy on this tick's signal evaluation.
          3. Evaluate the SMA signal and rebalance if it differs from
             current position and cooldown isn't set for this tick.

        Order persistence: we only update current_state in the DB AFTER the
        exchange order succeeds. A failed buy (e.g. min-notional reject)
        leaves the DB consistent with the exchange.
        """
        if not await self.is_enabled():
            log.info("simple_bot.tick.disabled")
            return None

        current = await self.current_state()
        meta = await self._get_meta()
        peak = Decimal(meta.get("peak_since_entry", "0") or "0")
        # Read AND clear the cooldown flag. If the previous tick set it, this
        # tick is the "one tick of cooldown" the trailing stop bought us. We
        # don't carry it past this iteration — that was the bug that locked
        # us out of re-entries during sharp drops that the SMA didn't catch.
        carried_cooldown = meta.get("stop_cooldown") == "true"
        if carried_cooldown:
            await self._set_meta(stop_cooldown="false")
        # `block_entry_this_tick` is True if either we just stopped out this
        # tick OR the previous tick passed us the cooldown flag.
        block_entry_this_tick = carried_cooldown

        # Fresh price for stop check + signal eval.
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
                fill = await go_out(self.exchange, self.db, symbol=self.symbol)
                if fill is not None:
                    await self._set_meta(
                        current_state=TrendState.OUT.value,
                        peak_since_entry="0",
                        stop_cooldown="true",  # block re-entry on next tick
                    )
                    current = TrendState.OUT
                    block_entry_this_tick = True
                    self._notify(
                        "Trailing stop fired",
                        f"Sold {fill.filled_qty} {self.symbol.split('/')[0]} @ "
                        f"${fill.avg_price:,.2f} (peak ${peak:,.2f} → trigger)",
                    )
                else:
                    log.warning("simple_bot.tick.trailing_stop.exit_failed")

        signal = await self.evaluate()

        if signal.state == current:
            log.info("simple_bot.tick.no_change", state=current.value)
            return signal

        if signal.state == TrendState.IN and block_entry_this_tick:
            log.info("simple_bot.tick.entry_blocked_by_cooldown")
            return signal

        # Rebalance. Only update DB state if the order succeeded — keeps DB
        # consistent with the exchange when an order is rejected (min-notional,
        # insufficient balance, etc.).
        base, quote = self.symbol.split("/", maxsplit=1)
        if signal.state == TrendState.IN:
            fill = await go_in(self.exchange, self.db, symbol=self.symbol)
            if fill is None:
                log.warning("simple_bot.tick.go_in_failed")
                self._notify(
                    "Order rejected",
                    f"Buy {base} failed (likely min-notional or balance).",
                )
                return signal
            entry_price = fill.avg_price if fill.avg_price > 0 else (current_price or signal.close)
            await self._set_meta(
                current_state=TrendState.IN.value,
                peak_since_entry=str(entry_price),
                stop_cooldown="false",
            )
            self._notify(
                f"{self.symbol} → IN",
                f"Bought {fill.filled_qty} {base} @ ${fill.avg_price:,.2f}",
            )
        else:
            fill = await go_out(self.exchange, self.db, symbol=self.symbol)
            if fill is None:
                log.warning("simple_bot.tick.go_out_failed")
                self._notify(
                    "Order rejected",
                    f"Sell {base} failed (likely min-notional or balance).",
                )
                return signal
            await self._set_meta(
                current_state=TrendState.OUT.value, peak_since_entry="0"
            )
            self._notify(
                f"{self.symbol} → OUT",
                f"Sold {fill.filled_qty} {base} @ ${fill.avg_price:,.2f}",
            )
        log.info(
            "simple_bot.tick.flipped",
            from_=current.value,
            to=signal.state.value,
            reason=signal.reason,
        )
        return signal

    def _notify(self, title: str, message: str) -> None:
        """Wrap the notifier so a buggy notifier never crashes a trade."""
        try:
            self.notifier(title, message)
        except Exception as e:
            log.warning("simple_bot.notify.error", error=str(e))

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
        try:
            balances = await self.exchange.fetch_balances()
        except Exception as e:
            log.warning("simple_bot.status.no_balances", error=str(e))
            balances = {}
        base, quote = self.symbol.split("/", maxsplit=1)
        base_bal = balances.get(f"spot:{base}")
        quote_bal = balances.get(f"spot:{quote}")
        # Try fresh price; on failure fall back to the most recent cached
        # ticker on the exchange object, then to the last persisted snapshot.
        last_price = Decimal("0")
        try:
            t = await self.exchange.fetch_ticker(self.symbol, "spot")
            last_price = t.last or (t.bid + t.ask) / 2 if t.ask else t.last
        except Exception as e:
            log.warning("simple_bot.status.no_price", error=str(e))
            cached = getattr(self.exchange, "_tickers", {}).get((self.symbol, "spot"))
            if cached and cached.last:
                last_price = cached.last
            elif self._last_signal and self._last_signal.close > 0:
                last_price = self._last_signal.close
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
