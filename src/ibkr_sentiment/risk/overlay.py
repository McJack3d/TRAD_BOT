"""Risk overlay for the IBKR sentiment bot.

Two checks the rest of the bot defers to:

  * `RiskOverlay.check_target()` — pre-trade gate. Veto any single
    target position that would violate the per-name, gross-exposure,
    or net-exposure caps. Run BEFORE orders are placed.

  * `RiskOverlay.check_account()` — continuous gate. Compare current
    NLV against the starting equity / daily anchor and trip the daily
    or cumulative loss stop if breached.

The overlay never sends orders itself — it returns a verdict and a
human-readable reason; the execution engine is responsible for acting
on it. That separation is what makes the risk module straightforward
to test in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from src.ibkr_sentiment.broker.base import AccountSummary, PositionView
from src.ibkr_sentiment.config import RiskOverlayConfig
from src.ibkr_sentiment.signal_engine.dollar_neutral import TargetPosition


@dataclass(slots=True)
class RiskVerdict:
    ok: bool
    reason: str


@dataclass
class RiskOverlay:
    cfg: RiskOverlayConfig
    starting_equity: Decimal
    daily_anchor: Decimal | None = None  # NLV at the start of the trading day

    def check_account(
        self, account: AccountSummary, *, daily_pnl: Decimal | None = None
    ) -> RiskVerdict:
        """Trip if daily or cumulative drawdown limits are breached."""
        nlv = account.net_liquidation
        cum_loss = self.starting_equity - nlv
        cum_pct = (
            cum_loss / self.starting_equity if self.starting_equity > 0 else Decimal("0")
        )
        if cum_pct >= self.cfg.cumulative_loss_stop_pct:
            return RiskVerdict(
                False,
                f"cumulative drawdown {float(cum_pct):.2%} >= cap "
                f"{float(self.cfg.cumulative_loss_stop_pct):.2%}",
            )
        if daily_pnl is not None and self.starting_equity > 0:
            daily_pct = (-daily_pnl) / self.starting_equity
            if daily_pct >= self.cfg.daily_loss_stop_pct:
                return RiskVerdict(
                    False,
                    f"daily drawdown {float(daily_pct):.2%} >= cap "
                    f"{float(self.cfg.daily_loss_stop_pct):.2%}",
                )
        return RiskVerdict(True, "account within risk limits")

    def check_target(
        self,
        target: TargetPosition,
        *,
        nlv: Decimal,
        proposed_basket: Iterable[TargetPosition],
    ) -> RiskVerdict:
        """Per-target check against the full proposed basket.

        Walks the basket once to compute gross / net exposure if THIS
        target were taken; rejects if either would breach the cap.
        """
        if nlv <= 0:
            return RiskVerdict(False, "non-positive NLV")
        per_name_cap = nlv * self.cfg.max_position_pct
        if abs(target.notional) > per_name_cap:
            return RiskVerdict(
                False,
                f"{target.symbol} notional {target.notional} > per-name cap {per_name_cap}",
            )
        gross = Decimal("0")
        net = Decimal("0")
        for t in proposed_basket:
            gross += abs(t.notional)
            net += t.notional if t.target_qty > 0 else -abs(t.notional)
        gross_cap = nlv * self.cfg.max_gross_exposure_pct
        net_cap = nlv * self.cfg.max_net_exposure_pct
        if gross > gross_cap:
            return RiskVerdict(
                False,
                f"proposed gross {gross} > gross cap {gross_cap}",
            )
        if abs(net) > net_cap:
            return RiskVerdict(
                False,
                f"proposed net {net} > net cap ±{net_cap}",
            )
        return RiskVerdict(True, "target within risk limits")

    def reconcile_positions(
        self,
        positions: list[PositionView],
        targets: list[TargetPosition],
    ) -> list[RiskVerdict]:
        """Sanity-check that no existing position is itself over-cap.

        Returns one verdict per offending position; an empty list means
        everything is within limits.
        """
        bad: list[RiskVerdict] = []
        target_by_sym = {t.symbol for t in targets}
        for p in positions:
            if p.symbol in target_by_sym:
                continue
            notional = abs(p.qty * p.mark_price)
            cap = self.starting_equity * self.cfg.max_position_pct
            if notional > cap:
                bad.append(
                    RiskVerdict(
                        False,
                        f"orphan position {p.symbol} notional {notional} > cap {cap}",
                    )
                )
        return bad
