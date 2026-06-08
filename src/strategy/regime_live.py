"""Regime-switching live execution engine daemon."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, UTC
from decimal import Decimal
import pandas as pd
import numpy as np
from sqlalchemy import select, update

from src.adapters.exchange_base import ExchangeAdapter, ExchangeOrder, Leg, Side
from src.logging_setup import log
from src.state.db import Database
from src.state.models import (
    Position,
    PositionStatus,
    Order,
    OrderStatus,
    Fill,
    SystemStatus,
    SystemStatusEnum,
    StateSnapshot,
    FundingPayment,
)
from src.state.pnl import build_state_snapshot, compute_realized_pnl
from src.strategy.regime_switch import (
    RegimeSwitchParams,
    SwitchPosition,
    SwitchSignal,
    Action,
    EntryLeg,
    evaluate_live,
)
from src.risk.perp_guards import (
    check_asset_cooloff,
    check_asset_daily_stop,
    check_consecutive_losses,
    check_account_daily_stop,
    check_account_cumulative_stop,
)
from src.execution.order import generate_client_order_id, round_qty



def time_until_next_bar_close(timeframe: str) -> float:
    now = time.time()
    tf_seconds = 60
    if timeframe.endswith("m"):
        tf_seconds = int(timeframe[:-1]) * 60
    elif timeframe.endswith("h"):
        tf_seconds = int(timeframe[:-1]) * 3600
    elif timeframe.endswith("d"):
        tf_seconds = int(timeframe[:-1]) * 86400

    next_close = ((now // tf_seconds) + 1) * tf_seconds
    return max(0.1, next_close - now - 1.0)


def start_of_utc_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


async def _safe_unrealized(exchange: ExchangeAdapter) -> Decimal:
    try:
        positions = await exchange.fetch_positions()
    except Exception as e:
        log.warning("regime_live.unrealized.fetch_failed", error=str(e))
        return Decimal("0")
    return sum((p.unrealized_pnl for p in positions), start=Decimal("0"))


class RegimeLiveBot:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        db: Database,
        symbols: list[str],
        config_path: str | None = None,
        notifier=None,
        mode: str = "paper",
    ):
        from src.notify import noop_notifier

        self.exchange = exchange
        self.db = db
        self.symbols = symbols
        self.notifier = notifier or noop_notifier
        self.mode = mode.upper()  # DRY_RUN, PAPER, LIVE
        self.running = False

        # Default parameters
        self.timeframe = "15m"
        self.risk_per_trade_pct = Decimal("0.01")
        self.max_leverage = Decimal("3.0")
        self.cooloff_bars = 6
        self.per_asset_daily_pct = Decimal("0.015")
        self.max_consecutive_losses = 4
        self.daily_loss_stop_pct = Decimal("0.02")
        self.cumulative_loss_stop_pct = Decimal("0.10")
        self.perp_taker_bps = Decimal("4.0")
        self.assumed_slippage_bps = Decimal("2.0")
        self.starting_equity_usdt = Decimal("1000.0")
        self.weekend_blackout = True
        self.macro_events: list[datetime] = []
        self.event_buffer_before_min = 30
        self.event_buffer_after_min = 30

        self.strategy_params = RegimeSwitchParams()
        self.symbol_configs = []

        if config_path:
            self.load_yaml_config(config_path)

    def load_yaml_config(self, config_path: str) -> None:
        import yaml
        with open(config_path) as f:
            cfg_data = yaml.safe_load(f)

        if "mode" in cfg_data:
            self.mode = str(cfg_data["mode"]).upper()
        if "starting_equity_usdt" in cfg_data:
            self.starting_equity_usdt = Decimal(str(cfg_data["starting_equity_usdt"]))
        elif "starting_equity_eur" in cfg_data:
            self.starting_equity_usdt = Decimal(str(cfg_data["starting_equity_eur"]))

        if "symbols" in cfg_data:
            from src.config import SymbolConfig
            symbols_list = []
            configs_list = []
            for sym_item in cfg_data["symbols"]:
                if isinstance(sym_item, dict):
                    cfg = SymbolConfig.model_validate(sym_item)
                    symbols_list.append(cfg.perp)
                    configs_list.append(cfg)
                else:
                    symbols_list.append(str(sym_item))
            self.symbols = symbols_list
            self.symbol_configs = configs_list

        strat_data = cfg_data.get("strategy", {})
        self.timeframe = strat_data.get("timeframe", self.timeframe)
        self.risk_per_trade_pct = Decimal(str(strat_data.get("risk_per_trade_pct", self.risk_per_trade_pct)))
        self.max_leverage = Decimal(str(strat_data.get("max_leverage", self.max_leverage)))

        for field in self.strategy_params.__slots__:
            if field in strat_data:
                setattr(self.strategy_params, field, strat_data[field])

        risk_data = cfg_data.get("risk", {})
        self.cooloff_bars = risk_data.get("cooloff_bars", self.cooloff_bars)
        self.per_asset_daily_pct = Decimal(str(risk_data.get("per_asset_daily_pct", self.per_asset_daily_pct)))
        self.max_consecutive_losses = risk_data.get("max_consecutive_losses", self.max_consecutive_losses)
        self.daily_loss_stop_pct = Decimal(str(risk_data.get("daily_loss_stop_pct", self.daily_loss_stop_pct)))
        self.cumulative_loss_stop_pct = Decimal(str(risk_data.get("cumulative_loss_stop_pct", self.cumulative_loss_stop_pct)))

        fees_data = cfg_data.get("fees", {})
        self.perp_taker_bps = Decimal(str(fees_data.get("perp_taker_bps", self.perp_taker_bps)))
        self.assumed_slippage_bps = Decimal(str(fees_data.get("assumed_slippage_bps", self.assumed_slippage_bps)))

    # ---- enable / disable -------------------------------------------

    async def enable(self) -> None:
        await self._set_meta(enabled="true")
        log.info("regime_live.enabled")

    async def disable(self) -> None:
        await self._set_meta(enabled="false")
        log.info("regime_live.disabled")

    async def is_enabled(self) -> bool:
        meta = await self._get_meta()
        return meta.get("enabled", "false") == "true"

    async def is_halted(self) -> bool:
        status_row = await self.db.get_status()
        return status_row.status == SystemStatusEnum.HALTED

    async def halt_trading(self, reason: str) -> None:
        log.error("regime_live.halt_trading", reason=reason)
        await self.db.set_status(SystemStatusEnum.HALTED, reason=reason)

        open_positions = await self.db.open_positions()
        for pos in open_positions:
            try:
                close_side = "sell" if pos.perp_qty > 0 else "buy"
                fill = await self._close_perp(pos.symbol, close_side, abs(pos.perp_qty), pos.id)
                if fill is not None:
                    realized_pnl = Decimal("0")
                    if pos.perp_qty > 0:
                        realized_pnl = (fill.avg_price - pos.perp_entry_price) * abs(pos.perp_qty)
                    else:
                        realized_pnl = (pos.perp_entry_price - fill.avg_price) * abs(pos.perp_qty)
                    await self.db.close_position(pos.id, realized_pnl=realized_pnl)
            except Exception as e:
                log.exception("regime_live.halt.close_failed", position_id=pos.id, error=str(e))
        self._notify("Bot Halted", reason)

    # ---- run / tick loop --------------------------------------------

    async def run_loop(self) -> None:
        self.running = True
        while self.running:
            sleep_seconds = time_until_next_bar_close(self.timeframe)
            log.info("regime_live.sleep", seconds=sleep_seconds)
            try:
                await asyncio.sleep(sleep_seconds)
                if not self.running:
                    break
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("regime_live.loop.error", error=str(e))
                await asyncio.sleep(5)

    async def tick(self) -> None:
        if await self.is_halted():
            log.info("regime_live.tick.halted")
            return

        if not await self.is_enabled():
            log.info("regime_live.tick.disabled")
            return

        now_utc = datetime.now(UTC)

        # 1. Check account-level risk guards
        daily_realized, cumulative_realized = await compute_realized_pnl(self.db, now_utc)
        total_unrealized = await _safe_unrealized(self.exchange)

        daily_stop_check = check_account_daily_stop(
            daily_realized,
            total_unrealized,
            self.starting_equity_usdt,
            self.daily_loss_stop_pct
        )
        if not daily_stop_check.ok:
            await self.halt_trading(daily_stop_check.reason)
            return

        cum_stop_check = check_account_cumulative_stop(
            cumulative_realized,
            self.starting_equity_usdt,
            self.cumulative_loss_stop_pct
        )
        if not cum_stop_check.ok:
            await self.halt_trading(cum_stop_check.reason)
            return

        async with self.db.session() as s:
            closed_positions = (
                await s.execute(
                    select(Position).where(Position.status == PositionStatus.CLOSED)
                )
            ).scalars().all()

        consecutive_losses_check = check_consecutive_losses(
            closed_positions,
            self.max_consecutive_losses
        )
        if not consecutive_losses_check.ok:
            await self.halt_trading(consecutive_losses_check.reason)
            return

        equity = self.starting_equity_usdt + cumulative_realized + total_unrealized

        # 2. Evaluate symbols
        for symbol in self.symbols:
            try:
                await self._evaluate_symbol(symbol, now_utc, equity, closed_positions)
            except Exception as e:
                log.exception("regime_live.tick.symbol_error", symbol=symbol, error=str(e))

        # 3. State snapshot update
        try:
            snap = await build_state_snapshot(
                self.db,
                self.exchange,
                self.starting_equity_usdt,
                now_utc
            )
            await self.db.add_snapshot(snap)
        except Exception as e:
            log.warning("regime_live.tick.snapshot_failed", error=str(e))

    # ---- symbol evaluation ------------------------------------------

    async def _evaluate_symbol(
        self,
        symbol: str,
        now_utc: datetime,
        equity: Decimal,
        closed_positions: list[Position]
    ) -> None:
        df = await self._fetch_ohlcv(symbol)
        if df.empty or len(df) < 200:
            log.warning("regime_live.evaluate.insufficient_bars", symbol=symbol, len=len(df))
            return

        current_bar_index = len(df) - 1
        close_price = Decimal(str(df["close"].iloc[-1]))
        high_price = Decimal(str(df["high"].iloc[-1]))
        low_price = Decimal(str(df["low"].iloc[-1]))

        db_pos = await self.get_active_position(symbol)
        meta = await self._get_meta()

        if db_pos is not None:
            side = 1 if db_pos.perp_qty > 0 else -1
            qty = abs(db_pos.perp_qty)
            entry_price = db_pos.perp_entry_price

            stop_price = float(meta.get(f"{symbol}_stop_price", "0"))
            entry_leg_str = meta.get(f"{symbol}_entry_leg", None)
            entry_leg = EntryLeg(entry_leg_str) if entry_leg_str else None
            atr_at_entry = float(meta.get(f"{symbol}_atr_at_entry", "0"))
            entry_equity = float(meta.get(f"{symbol}_entry_equity", "0"))
            entry_index = int(meta.get(f"{symbol}_entry_index", "-1"))

            pos = SwitchPosition(
                side=side,
                entry_price=float(entry_price),
                entry_leg=entry_leg,
                atr_at_entry=atr_at_entry,
                entry_index=entry_index,
                stop_price=stop_price,
                qty=float(qty),
                entry_equity=entry_equity,
            )
        else:
            pos = SwitchPosition.flat()

        try:
            ticker = await self.exchange.fetch_ticker(symbol, "perp")
            current_price = ticker.last or (ticker.bid + ticker.ask) / 2
        except Exception as e:
            log.warning("regime_live.ticker.failed", symbol=symbol, error=str(e))
            current_price = close_price

        # Intrabar or bar close Stop Loss checks
        if pos.side != 0:
            stop_hit = False
            reason = ""
            if pos.side == 1:
                if current_price <= Decimal(str(pos.stop_price)):
                    stop_hit = True
                    reason = f"stop hit: current price {current_price:.2f} <= stop {pos.stop_price:.2f}"
                elif low_price <= Decimal(str(pos.stop_price)):
                    stop_hit = True
                    reason = f"stop hit: low {low_price:.2f} <= stop {pos.stop_price:.2f}"
            elif pos.side == -1:
                if current_price >= Decimal(str(pos.stop_price)):
                    stop_hit = True
                    reason = f"stop hit: current price {current_price:.2f} >= stop {pos.stop_price:.2f}"
                elif high_price >= Decimal(str(pos.stop_price)):
                    stop_hit = True
                    reason = f"stop hit: high {high_price:.2f} >= stop {pos.stop_price:.2f}"

            if stop_hit:
                log.warning("regime_live.stop_hit", symbol=symbol, reason=reason)
                close_side = "sell" if pos.side == 1 else "buy"
                fill = await self._close_perp(symbol, close_side, Decimal(str(pos.qty)), db_pos.id)
                if fill is not None:
                    realized_pnl = Decimal("0")
                    if pos.side == 1:
                        realized_pnl = (fill.avg_price - db_pos.perp_entry_price) * abs(db_pos.perp_qty)
                    else:
                        realized_pnl = (db_pos.perp_entry_price - fill.avg_price) * abs(db_pos.perp_qty)

                    await self.db.close_position(db_pos.id, realized_pnl=realized_pnl)
                    await self._set_meta(**{
                        f"{symbol}_last_loss_exit_bar": str(current_bar_index)
                    })
                    self._notify(f"{symbol} Stop Out", reason)
                return

        sig = evaluate_live(df, pos, self.strategy_params)

        if sig.action == Action.HOLD:
            return

        elif sig.action == Action.EXIT:
            if db_pos is not None:
                close_side = "sell" if pos.side == 1 else "buy"
                fill = await self._close_perp(symbol, close_side, Decimal(str(pos.qty)), db_pos.id)
                if fill is not None:
                    realized_pnl = Decimal("0")
                    if pos.side == 1:
                        realized_pnl = (fill.avg_price - db_pos.perp_entry_price) * abs(db_pos.perp_qty)
                    else:
                        realized_pnl = (db_pos.perp_entry_price - fill.avg_price) * abs(db_pos.perp_qty)

                    await self.db.close_position(db_pos.id, realized_pnl=realized_pnl)
                    self._notify(f"{symbol} Exit", sig.reason)
            return

        elif sig.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
            if self.is_entry_blocked_by_calendar(now_utc):
                log.info("regime_live.entry_blocked.calendar", symbol=symbol)
                return

            closed_trades = []
            for p in closed_positions:
                if p.symbol == symbol:
                    exit_idx = None
                    exit_bar_str = meta.get(f"{symbol}_last_loss_exit_bar", None)
                    if exit_bar_str:
                        exit_idx = int(exit_bar_str)
                    elif p.closed_at is not None:
                        diffs = np.abs((df.index - pd.Timestamp(p.closed_at)).total_seconds())
                        if len(diffs) > 0:
                            exit_idx = int(np.argmin(diffs))

                    closed_trades.append({
                        "symbol": p.symbol,
                        "net_pnl": p.realized_pnl,
                        "exit_bar_index": exit_idx,
                        "exit_ts": p.closed_at,
                    })

            cooloff_check = check_asset_cooloff(
                symbol,
                closed_trades,
                current_bar_index,
                self.cooloff_bars
            )
            if not cooloff_check.ok:
                log.info("regime_live.entry_blocked.cooloff", symbol=symbol, reason=cooloff_check.reason)
                return

            asset_realized_pnl = await self._get_asset_daily_realized_pnl(symbol, start_of_utc_day(now_utc))
            asset_unrealized_pnl = await self._get_asset_unrealized_pnl(symbol)
            asset_daily_stop_check = check_asset_daily_stop(
                symbol,
                asset_realized_pnl,
                asset_unrealized_pnl,
                equity,
                self.per_asset_daily_pct
            )
            if not asset_daily_stop_check.ok:
                log.info("regime_live.entry_blocked.asset_daily_stop", symbol=symbol, reason=asset_daily_stop_check.reason)
                return

            from src.strategy.regime_switch import precompute as rs_precompute
            pre = rs_precompute(df, self.strategy_params)
            atr_now = Decimal(str(pre.atr[-1]))

            stop_distance = Decimal(str(self.strategy_params.atr_mult)) * atr_now
            if stop_distance <= 0:
                log.warning("regime_live.sizing.degenerate_atr", symbol=symbol, atr=atr_now)
                return

            risk_budget = equity * self.risk_per_trade_pct
            qty = risk_budget / stop_distance

            qty_step = Decimal("0.0001")
            min_qty = Decimal("0.0001")

            symbol_cfg = self._get_symbol_config(symbol)
            if symbol_cfg:
                qty_step = symbol_cfg.qty_step
                min_qty = symbol_cfg.min_qty

            max_qty = (equity * self.max_leverage) / current_price
            if qty > max_qty:
                qty = max_qty

            qty_rounded = round_qty(qty, qty_step)
            if qty_rounded < min_qty:
                log.info("regime_live.sizing.below_min_qty", symbol=symbol, qty=qty_rounded, min_qty=min_qty)
                return

            open_side = "buy" if sig.action == Action.ENTER_LONG else "sell"
            fill = await self._open_perp(symbol, open_side, qty_rounded)
            if fill is not None:
                signed_qty = fill.filled_qty if open_side == "buy" else -fill.filled_qty
                pos_row = Position(
                    symbol=symbol,
                    status=PositionStatus.OPEN,
                    spot_qty=Decimal("0"),
                    perp_qty=signed_qty,
                    spot_entry_price=Decimal("0"),
                    perp_entry_price=fill.avg_price,
                    initial_margin=(fill.filled_qty * fill.avg_price) / self.max_leverage,
                    opened_at=datetime.now(UTC),
                )
                db_pos = await self.db.create_position(pos_row)

                await self._set_meta(**{
                    f"{symbol}_stop_price": str(sig.stop_price),
                    f"{symbol}_entry_leg": sig.leg.value if sig.leg else "",
                    f"{symbol}_atr_at_entry": str(pre.atr[-1]),
                    f"{symbol}_entry_equity": str(equity),
                    f"{symbol}_entry_index": str(current_bar_index),
                })
                self._notify(f"{symbol} Entry", f"Entered {sig.action.value} {qty_rounded} @ ${fill.avg_price:,.2f}")

    # ---- order execution helpers ------------------------------------

    async def _open_perp(self, symbol: str, side: Side, qty: Decimal) -> ExchangeOrder | None:
        if self.mode != "DRY_RUN":
            try:
                await self.exchange.set_leverage(symbol, int(self.max_leverage))
            except Exception as e:
                log.warning("regime_live.set_leverage.failed", symbol=symbol, error=str(e))

        client_id = generate_client_order_id(prefix=f"p{side[0]}")
        order_row = await self.db.add_order(
            Order(
                client_order_id=client_id,
                symbol=symbol,
                leg="perp",
                side=side,
                qty=qty,
                status=OrderStatus.NEW,
            )
        )

        if self.mode == "DRY_RUN":
            ticker = await self.exchange.fetch_ticker(symbol, "perp")
            mid = ticker.last or (ticker.bid + ticker.ask) / 2
            slip = mid * self.assumed_slippage_bps / Decimal("10000.0")
            fill_price = mid + slip if side == "buy" else mid - slip
            fee = qty * fill_price * self.perp_taker_bps / Decimal("10000.0")

            result = ExchangeOrder(
                client_order_id=client_id,
                exchange_order_id=f"dry-{client_id}",
                symbol=symbol,
                leg="perp",
                side=side,
                qty=qty,
                filled_qty=qty,
                avg_price=fill_price,
                status="filled",
                fee_paid=fee,
                fee_asset="USDT",
                ts=datetime.now(UTC),
            )
        else:
            try:
                result = await self.exchange.submit_order(
                    symbol=symbol,
                    leg="perp",
                    side=side,
                    qty=qty,
                    client_order_id=client_id,
                    reduce_only=False,
                )
            except Exception as e:
                log.exception("regime_live.submit.failed", symbol=symbol, side=side, error=str(e))
                await self.db.update_order_status(client_id, OrderStatus.REJECTED)
                return None

        status = OrderStatus.FILLED if result.filled_qty >= qty else OrderStatus.PARTIALLY_FILLED
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
                    order_id=order_row.id,
                    exchange_trade_id=result.exchange_order_id or client_id,
                    qty=result.filled_qty,
                    price=result.avg_price,
                    fee=result.fee_paid,
                    fee_asset=result.fee_asset,
                    ts=datetime.now(UTC),
                )
            )
        return result

    async def _close_perp(self, symbol: str, side: Side, qty: Decimal, position_id: int) -> ExchangeOrder | None:
        client_id = generate_client_order_id(prefix=f"p{side[0]}")
        order_row = await self.db.add_order(
            Order(
                client_order_id=client_id,
                symbol=symbol,
                leg="perp",
                side=side,
                qty=qty,
                status=OrderStatus.NEW,
                position_id=position_id,
            )
        )

        if self.mode == "DRY_RUN":
            ticker = await self.exchange.fetch_ticker(symbol, "perp")
            mid = ticker.last or (ticker.bid + ticker.ask) / 2
            slip = mid * self.assumed_slippage_bps / Decimal("10000.0")
            fill_price = mid + slip if side == "buy" else mid - slip
            fee = qty * fill_price * self.perp_taker_bps / Decimal("10000.0")

            result = ExchangeOrder(
                client_order_id=client_id,
                exchange_order_id=f"dry-{client_id}",
                symbol=symbol,
                leg="perp",
                side=side,
                qty=qty,
                filled_qty=qty,
                avg_price=fill_price,
                status="filled",
                fee_paid=fee,
                fee_asset="USDT",
                ts=datetime.now(UTC),
            )
        else:
            try:
                result = await self.exchange.submit_order(
                    symbol=symbol,
                    leg="perp",
                    side=side,
                    qty=qty,
                    client_order_id=client_id,
                    reduce_only=True,
                )
            except Exception as e:
                log.exception("regime_live.submit_close.failed", symbol=symbol, side=side, error=str(e))
                await self.db.update_order_status(client_id, OrderStatus.REJECTED)
                return None

        status = OrderStatus.FILLED if result.filled_qty >= qty else OrderStatus.PARTIALLY_FILLED
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
                    order_id=order_row.id,
                    exchange_trade_id=result.exchange_order_id or client_id,
                    qty=result.filled_qty,
                    price=result.avg_price,
                    fee=result.fee_paid,
                    fee_asset=result.fee_asset,
                    ts=datetime.now(UTC),
                )
            )
        return result

    # ---- utility / db helpers ----------------------------------------

    async def get_active_position(self, symbol: str) -> Position | None:
        async with self.db.session() as s:
            res = await s.execute(
                select(Position).where(
                    (Position.symbol == symbol) &
                    (Position.status == PositionStatus.OPEN)
                )
            )
            return res.scalar_one_or_none()

    async def _get_meta(self) -> dict[str, str]:
        async with self.db.session() as s:
            row = (await s.execute(select(SystemStatus).where(SystemStatus.id == 1))).scalar_one_or_none()
            raw = row.halt_reason if row else None
        if not raw:
            return {}
        out = {}
        for chunk in raw.split("|"):
            if ":" in chunk:
                k, v = chunk.rsplit(":", 1)
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

    def _get_symbol_config(self, symbol: str):
        if self.symbol_configs:
            for s in self.symbol_configs:
                if s.perp == symbol or s.spot == symbol:
                    return s
        return None

    def is_entry_blocked_by_calendar(self, now: datetime) -> bool:
        if self.weekend_blackout:
            weekday = now.weekday()
            if weekday == 5:
                return True
            if weekday == 6 and now.hour < 12:
                return True

        if self.macro_events:
            for event_dt in self.macro_events:
                delta = (now - event_dt).total_seconds() / 60.0
                if -self.event_buffer_before_min <= delta <= self.event_buffer_after_min:
                    return True
        return False

    async def _get_asset_daily_realized_pnl(self, symbol: str, sod: datetime) -> Decimal:
        async with self.db.session() as s:
            res = await s.execute(
                select(Position).where(
                    (Position.symbol == symbol)
                    & (Position.status == PositionStatus.CLOSED)
                    & (Position.closed_at >= sod)
                )
            )
            pos_pnl = sum((p.realized_pnl for p in res.scalars().all()), start=Decimal("0"))

            res_funding = await s.execute(
                select(FundingPayment).where(
                    (FundingPayment.symbol == symbol)
                    & (FundingPayment.funding_time >= sod)
                )
            )
            funding_pnl = sum((fp.payment for fp in res_funding.scalars().all()), start=Decimal("0"))

            return pos_pnl + funding_pnl

    async def _get_asset_unrealized_pnl(self, symbol: str) -> Decimal:
        try:
            positions = await self.exchange.fetch_positions()
            for p in positions:
                if p.symbol == symbol:
                    return p.unrealized_pnl
        except Exception as e:
            log.warning("regime_live.unrealized.fetch_failed", symbol=symbol, error=str(e))
        return Decimal("0")

    async def _fetch_ohlcv(self, symbol: str) -> pd.DataFrame:
        override = getattr(self, "df_override", None)
        if override is not None:
            return override

        ccxt_perp = getattr(self.exchange, "perp", None)
        if ccxt_perp is None:
            raise RuntimeError("Exchange has no .perp CCXT client and no df_override is set")

        last_exc = None
        limit = 300
        for attempt in range(3):
            try:
                ohlcv = await ccxt_perp.fetch_ohlcv(symbol, self.timeframe, limit=limit)
                df = pd.DataFrame(
                    ohlcv,
                    columns=["timestamp", "open", "high", "low", "close", "volume"]
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df.set_index("timestamp", inplace=True)
                return df
            except Exception as e:
                last_exc = e
                log.warning("regime_live.fetch_ohlcv.retry", attempt=attempt+1, symbol=symbol, error=str(e))
                await asyncio.sleep(1.5**attempt)
        assert last_exc is not None
        raise last_exc

    def _notify(self, title: str, message: str) -> None:
        try:
            self.notifier(title, message)
        except Exception as e:
            log.warning("regime_live.notify.error", error=str(e))
