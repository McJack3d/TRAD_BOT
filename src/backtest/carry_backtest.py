"""Two-sided funding-carry backtester.

Replays a funding-rate series (per 8h) and a borrow-rate series (APR,
daily, forward-filled onto the 8h grid) through the *pure* decision
functions in `src.strategy.funding_carry`, so the backtest and the live
bot share one brain. No network, no I/O — the caller supplies the series
(from `src.data.history`) and gets back per-leg attribution + the
acceptance-gate inputs the spec (§6) requires.

What it models, honestly:

  * Carry income per held 8h settlement — `funding` on the positive leg,
    `|funding| − borrow_8h` on the negative leg (net of borrow, the whole
    point of the strategy).
  * Round-trip cost on open and close: a delta-neutral position trades
    two instruments (spot + perp), so the one-way cost is
    `2 × (fee_bps + slippage_bps)`, charged again on exit.
  * Per-leg standalone equity curves, so the negative leg's contribution
    is measured *on its own* — flat when idle, which honestly penalises a
    leg whose opportunity is rare. That is the number the gate checks.

What it deliberately ignores: spot/perp basis PnL (delta-neutral, it
cancels to first order) and price-path liquidation (a risk-overlay
concern, modelled in the execution layer, not the carry economics).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd

from src.strategy.funding_carry import (
    SETTLEMENTS_PER_YEAR,
    CarryConfig,
    CarryPosition,
    CarrySide,
    borrow_rate_per_8h,
    evaluate_both_legs,
)


@dataclass(slots=True)
class CarryEpisode:
    symbol: str
    side: CarrySide
    entry_ts: datetime
    exit_ts: datetime
    n_settlements: int
    net_return: float  # fraction, incl. entry + exit cost


@dataclass(slots=True)
class LegStats:
    side: CarrySide
    n_episodes: int
    settlements_held: int
    total_return: float
    sharpe: float
    max_drawdown: float


@dataclass(slots=True)
class CarryBacktestResult:
    symbol: str
    initial_equity: Decimal
    final_equity: Decimal
    equity_curve: pd.Series  # ts-indexed combined equity
    positive: LegStats
    negative: LegStats
    combined_sharpe: float
    max_drawdown: float
    episodes: list[CarryEpisode] = field(default_factory=list)

    @property
    def total_return(self) -> float:
        if self.initial_equity == 0:
            return 0.0
        return float(self.final_equity / self.initial_equity) - 1.0


# ---- metric helpers ---------------------------------------------------


def _sharpe(returns: list[float]) -> float:
    """Annualised Sharpe of a per-8h-settlement return stream. Idle
    settlements (return 0) are included on purpose — a leg that trades
    rarely should not be flattered by ignoring the time it sits flat."""
    if not returns:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sd = arr.std(ddof=0)
    if sd == 0:
        return 0.0
    return float(arr.mean() / sd * math.sqrt(SETTLEMENTS_PER_YEAR))


def _max_drawdown(returns: list[float]) -> float:
    """Max drawdown (≤ 0) of the equity curve implied by a return stream."""
    if not returns:
        return 0.0
    equity = np.cumprod(1.0 + np.asarray(returns, dtype=float))
    peaks = np.maximum.accumulate(equity)
    return float((equity / peaks - 1.0).min())


def _total_return(returns: list[float]) -> float:
    if not returns:
        return 0.0
    return float(np.prod(1.0 + np.asarray(returns, dtype=float)) - 1.0)


# ---- the backtest -----------------------------------------------------


def _align_borrow(funding: pd.Series, borrow_apr: pd.Series, cap: Decimal) -> pd.Series:
    """Forward/back-fill the daily borrow series onto the 8h funding grid.
    Where borrow is unknown the cap is used — which makes the negative-leg
    decision refuse entry (no data ⇒ no trade), the honest default."""
    if borrow_apr is None or borrow_apr.empty:
        return pd.Series(float(cap), index=funding.index)
    aligned = borrow_apr.reindex(funding.index.union(borrow_apr.index)).sort_index()
    aligned = aligned.ffill().bfill().reindex(funding.index)
    return aligned.fillna(float(cap))


def backtest_carry(
    funding: pd.Series,
    borrow_apr: pd.Series | None,
    symbol: str,
    *,
    cfg: CarryConfig | None = None,
    initial_equity: Decimal = Decimal("1000"),
    fee_bps: Decimal = Decimal("4.0"),
    slippage_bps: Decimal = Decimal("2.0"),
    deploy_fraction: float = 1.0,
) -> CarryBacktestResult:
    """Replay `funding` (per-8h) + `borrow_apr` (APR) for `symbol`.

    Returns combined and per-leg stats. The per-leg curves are standalone
    (flat when that leg is idle) so the negative leg is judged on its own
    contribution, exactly as the acceptance gate requires.
    """
    cfg = cfg or CarryConfig()
    funding = funding.sort_index()
    if funding.empty:
        zero_leg = lambda s: LegStats(s, 0, 0, 0.0, 0.0, 0.0)  # noqa: E731
        return CarryBacktestResult(
            symbol, initial_equity, initial_equity,
            pd.Series(dtype=float),
            zero_leg(CarrySide.POSITIVE), zero_leg(CarrySide.NEGATIVE),
            0.0, 0.0, [],
        )

    borrow = _align_borrow(funding, borrow_apr, cfg.max_borrow_rate_apr)

    # One-way cost fraction: two instruments (spot + perp) per leg.
    one_way_cost = float(
        Decimal("2") * (fee_bps + slippage_bps) / Decimal("10000")
    )

    equity = float(initial_equity)
    position: CarryPosition | None = None
    ep_entry_ts: pd.Timestamp | None = None
    ep_settlements = 0
    ep_entry_equity = 0.0

    pos_returns: list[float] = []
    neg_returns: list[float] = []
    all_returns: list[float] = []
    pos_held = 0
    neg_held = 0
    episodes: list[CarryEpisode] = []
    curve_idx: list[pd.Timestamp] = []
    curve_val: list[float] = []

    for ts, f_val in funding.items():
        f = Decimal(str(f_val))
        b = Decimal(str(borrow.loc[ts]))
        now = ts.to_pydatetime()

        held_side = position.side if position is not None else None
        pos_r = 0.0
        neg_r = 0.0

        # 1) Accrue carry for the position held *entering* this settlement.
        if held_side == CarrySide.POSITIVE:
            r = float(f) * deploy_fraction
            equity *= 1.0 + r
            pos_r += r
            pos_held += 1
            ep_settlements += 1
        elif held_side == CarrySide.NEGATIVE:
            r = float(-f - borrow_rate_per_8h(b)) * deploy_fraction  # |f| − borrow_8h
            equity *= 1.0 + r
            neg_r += r
            neg_held += 1
            ep_settlements += 1

        # 2) Decision for the next interval.
        sig = evaluate_both_legs(symbol, f, b, position, cfg, now)

        if sig.action == "enter" and position is None and sig.side is not None:
            ep_entry_equity = equity
            equity *= 1.0 - one_way_cost
            if sig.side == CarrySide.POSITIVE:
                pos_r -= one_way_cost
            else:
                neg_r -= one_way_cost
            position = CarryPosition(symbol, sig.side, now, Decimal(str(equity)))
            ep_entry_ts, ep_settlements = ts, 0
        elif sig.action == "exit" and position is not None:
            equity *= 1.0 - one_way_cost
            if position.side == CarrySide.POSITIVE:
                pos_r -= one_way_cost
            else:
                neg_r -= one_way_cost
            episodes.append(
                CarryEpisode(
                    symbol=symbol,
                    side=position.side,
                    entry_ts=ep_entry_ts.to_pydatetime() if ep_entry_ts else now,
                    exit_ts=now,
                    n_settlements=ep_settlements,
                    net_return=(equity / ep_entry_equity - 1.0)
                    if ep_entry_equity
                    else 0.0,
                )
            )
            position = None

        pos_returns.append(pos_r)
        neg_returns.append(neg_r)
        all_returns.append(pos_r + neg_r)
        curve_idx.append(ts)
        curve_val.append(equity)

    equity_curve = pd.Series(curve_val, index=pd.DatetimeIndex(curve_idx), name="equity")

    positive = LegStats(
        side=CarrySide.POSITIVE,
        n_episodes=sum(1 for e in episodes if e.side == CarrySide.POSITIVE),
        settlements_held=pos_held,
        total_return=_total_return(pos_returns),
        sharpe=_sharpe(pos_returns),
        max_drawdown=_max_drawdown(pos_returns),
    )
    negative = LegStats(
        side=CarrySide.NEGATIVE,
        n_episodes=sum(1 for e in episodes if e.side == CarrySide.NEGATIVE),
        settlements_held=neg_held,
        total_return=_total_return(neg_returns),
        sharpe=_sharpe(neg_returns),
        max_drawdown=_max_drawdown(neg_returns),
    )
    return CarryBacktestResult(
        symbol=symbol,
        initial_equity=initial_equity,
        final_equity=Decimal(str(equity)),
        equity_curve=equity_curve,
        positive=positive,
        negative=negative,
        combined_sharpe=_sharpe(all_returns),
        max_drawdown=_max_drawdown(all_returns),
        episodes=episodes,
    )


# ---- acceptance gates (spec §6) --------------------------------------


@dataclass(slots=True)
class Gate:
    name: str
    passed: bool
    detail: str


def carry_acceptance_gates(result: CarryBacktestResult) -> list[Gate]:
    """The four gates the negative leg must clear before paper (spec §6).
    Returns one Gate per criterion; the build STOPS unless all pass."""
    neg = result.negative
    pos = result.positive
    return [
        Gate(
            "negative leg net-positive after borrow + fees",
            neg.total_return > 0,
            f"negative-leg total return {neg.total_return:+.2%}",
        ),
        Gate(
            "negative-leg Sharpe > 1.0 (own contribution)",
            neg.sharpe > 1.0,
            f"negative-leg Sharpe {neg.sharpe:.2f}",
        ),
        Gate(
            "≥ 20 distinct negative-funding episodes traded",
            neg.n_episodes >= 20,
            f"{neg.n_episodes} negative-leg episodes",
        ),
        Gate(
            "combined max DD ≤ one-sided max DD + 5pp",
            result.max_drawdown >= pos.max_drawdown - 0.05,
            f"combined DD {result.max_drawdown:.2%} vs positive-only "
            f"{pos.max_drawdown:.2%} (budget {pos.max_drawdown - 0.05:.2%})",
        ),
    ]


def gates_summary(gates: list[Gate]) -> str:
    lines = [
        f"  [{'PASS' if g.passed else 'FAIL'}] {g.name}\n         {g.detail}"
        for g in gates
    ]
    verdict = "ALL GATES PASS" if all(g.passed for g in gates) else "GATES FAILED"
    return f"{verdict}\n" + "\n".join(lines)
