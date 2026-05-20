"""Event-driven backtester.

Iterates funding-rate events chronologically. At each event:
- Apply funding to any open position (mark-to-market).
- Evaluate signal: enter / exit / hold using the same `signals.py` that
  the live bot uses.
- Charge realistic fees + slippage on entry / exit.

Inputs: per-symbol Parquet of (ts, funding_rate, mark_price), produced
by `data/historical.py`. OHLCV is used only for fee/slippage reference
prices if available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from src.config import BotConfig
from src.data.historical import load_funding
from src.strategy.signals import (
    EntrySignal,
    ExitSignal,
    PositionView,
    evaluate_signal,
)


@dataclass
class BacktestTrade:
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    notional: Decimal
    funding_collected: Decimal
    fees_paid: Decimal
    slippage_paid: Decimal
    net_pnl: Decimal


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame
    trades: list[BacktestTrade] = field(default_factory=list)
    initial_equity: Decimal = Decimal("0")
    final_equity: Decimal = Decimal("0")


class BacktestEngine:
    def __init__(self, cfg: BotConfig, data_dir: str = "data/history"):
        self.cfg = cfg
        self.data_dir = data_dir

    def run(
        self,
        start: datetime,
        end: datetime,
        initial_equity: Decimal,
    ) -> BacktestResult:
        events = self._load_events(start, end)
        if events.empty:
            return BacktestResult(
                equity_curve=pd.DataFrame(columns=["ts", "equity"]),
                initial_equity=initial_equity,
                final_equity=initial_equity,
            )

        equity = initial_equity
        open_positions: dict[str, _OpenPos] = {}
        trades: list[BacktestTrade] = []
        equity_points: list[tuple[datetime, Decimal]] = []
        n_symbols = max(1, len(self.cfg.symbols))
        fee_bps = self.cfg.fees.spot_taker_bps + self.cfg.fees.perp_taker_bps
        slip_bps = self.cfg.fees.assumed_slippage_bps

        for row in events.itertuples(index=False):
            ts: datetime = row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts
            symbol: str = row.symbol
            funding_rate: Decimal = Decimal(str(row.funding_rate))

            # Apply funding to existing position (short receives positive funding).
            pos = open_positions.get(symbol)
            if pos is not None:
                fp = pos.notional * funding_rate
                pos.funding_collected += fp
                equity += fp

            position_view = (
                PositionView(symbol=symbol, opened_at=pos.opened_at, notional=pos.notional)
                if pos
                else None
            )
            target = (equity * self.cfg.risk.max_gross_notional_pct) / n_symbols

            signal = evaluate_signal(
                symbol=symbol,
                funding_rate=funding_rate,
                cfg=self.cfg.strategy,
                position=position_view,
                proposed_notional=target,
                now=ts,
            )

            if isinstance(signal, EntrySignal) and pos is None:
                entry_cost = signal.notional * (fee_bps + slip_bps) / Decimal("10000")
                equity -= entry_cost
                open_positions[symbol] = _OpenPos(
                    symbol=symbol,
                    opened_at=ts,
                    notional=signal.notional,
                    fees_paid=entry_cost,
                    slippage_paid=signal.notional * slip_bps / Decimal("10000"),
                )
            elif isinstance(signal, ExitSignal) and pos is not None:
                exit_cost = pos.notional * (fee_bps + slip_bps) / Decimal("10000")
                equity -= exit_cost
                net = pos.funding_collected - pos.fees_paid - exit_cost
                trades.append(
                    BacktestTrade(
                        symbol=symbol,
                        entry_ts=pos.opened_at,
                        exit_ts=ts,
                        notional=pos.notional,
                        funding_collected=pos.funding_collected,
                        fees_paid=pos.fees_paid + exit_cost,
                        slippage_paid=pos.slippage_paid + (pos.notional * slip_bps / Decimal("10000")),
                        net_pnl=net,
                    )
                )
                del open_positions[symbol]

            equity_points.append((ts, equity))

        # Force-close any still-open positions at the end of the window.
        for symbol, pos in list(open_positions.items()):
            exit_cost = pos.notional * (fee_bps + slip_bps) / Decimal("10000")
            equity -= exit_cost
            trades.append(
                BacktestTrade(
                    symbol=symbol,
                    entry_ts=pos.opened_at,
                    exit_ts=end,
                    notional=pos.notional,
                    funding_collected=pos.funding_collected,
                    fees_paid=pos.fees_paid + exit_cost,
                    slippage_paid=pos.slippage_paid,
                    net_pnl=pos.funding_collected - pos.fees_paid - exit_cost,
                )
            )

        equity_df = pd.DataFrame(
            [{"ts": ts, "equity": float(eq)} for ts, eq in equity_points]
        )
        return BacktestResult(
            equity_curve=equity_df,
            trades=trades,
            initial_equity=initial_equity,
            final_equity=equity,
        )

    def _load_events(self, start: datetime, end: datetime) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for sc in self.cfg.symbols:
            df = load_funding(sc.spot, self.data_dir)
            if df.empty:
                continue
            df = df.copy()
            df["symbol"] = sc.spot
            frames.append(df)
        if not frames:
            return pd.DataFrame(columns=["ts", "symbol", "funding_rate", "mark_price"])
        all_df = pd.concat(frames)
        all_df["ts"] = pd.to_datetime(all_df["ts"], utc=True)
        start_ts = pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end).tz_convert("UTC") if pd.Timestamp(end).tzinfo else pd.Timestamp(end, tz="UTC")
        all_df = all_df[(all_df["ts"] >= start_ts) & (all_df["ts"] < end_ts)]
        return all_df.sort_values("ts").reset_index(drop=True)


@dataclass
class _OpenPos:
    symbol: str
    opened_at: datetime
    notional: Decimal
    fees_paid: Decimal = Decimal("0")
    slippage_paid: Decimal = Decimal("0")
    funding_collected: Decimal = Decimal("0")
