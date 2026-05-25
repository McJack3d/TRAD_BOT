"""Tests for the IBKR sentiment bot's risk overlay."""

from __future__ import annotations

from decimal import Decimal

from src.ibkr_sentiment.broker.base import AccountSummary
from src.ibkr_sentiment.config import RiskOverlayConfig
from src.ibkr_sentiment.risk.overlay import RiskOverlay
from src.ibkr_sentiment.signal_engine.dollar_neutral import TargetPosition
from src.ibkr_sentiment.signal_engine.mapping import Side


def _overlay(equity: Decimal = Decimal("10000")) -> RiskOverlay:
    return RiskOverlay(
        cfg=RiskOverlayConfig(
            starting_equity_usd=equity,
            max_gross_exposure_pct=Decimal("1.0"),
            max_net_exposure_pct=Decimal("0.2"),
            max_position_pct=Decimal("0.1"),
            daily_loss_stop_pct=Decimal("0.02"),
            cumulative_loss_stop_pct=Decimal("0.1"),
        ),
        starting_equity=equity,
    )


def _acct(nlv: Decimal) -> AccountSummary:
    return AccountSummary(
        net_liquidation=nlv,
        available_funds=nlv,
        gross_position_value=Decimal("0"),
    )


def test_account_check_trips_on_cumulative_drawdown():
    o = _overlay(Decimal("10000"))
    v = o.check_account(_acct(Decimal("8500")))  # 15% drawdown
    assert v.ok is False
    assert "cumulative" in v.reason


def test_account_check_passes_within_limits():
    o = _overlay(Decimal("10000"))
    v = o.check_account(_acct(Decimal("9800")))
    assert v.ok is True


def test_account_check_trips_on_daily_drawdown():
    o = _overlay(Decimal("10000"))
    v = o.check_account(_acct(Decimal("9900")), daily_pnl=Decimal("-250"))
    assert v.ok is False
    assert "daily" in v.reason


def test_target_check_rejects_oversized_position():
    o = _overlay(Decimal("10000"))
    target = TargetPosition(
        symbol="AAPL", side=Side.LONG, target_qty=Decimal("20"),
        notional=Decimal("5000"),  # 50% of equity — over per-name cap (10%)
        reason="",
    )
    v = o.check_target(target, nlv=Decimal("10000"), proposed_basket=[target])
    assert v.ok is False
    assert "per-name" in v.reason


def test_target_check_rejects_when_gross_exceeds_cap():
    o = _overlay(Decimal("10000"))
    basket = [
        TargetPosition(
            symbol=f"S{i}", side=Side.LONG, target_qty=Decimal("1"),
            notional=Decimal("900"), reason="",
        )
        for i in range(15)
    ]
    target = basket[0]
    v = o.check_target(target, nlv=Decimal("10000"), proposed_basket=basket)
    assert v.ok is False
    assert "gross" in v.reason


def test_target_check_rejects_when_net_exceeds_cap():
    o = _overlay(Decimal("10000"))
    # 5 longs * $500 notional = $2500 net (25% > 20% cap)
    basket = [
        TargetPosition(
            symbol=f"S{i}", side=Side.LONG, target_qty=Decimal("5"),
            notional=Decimal("500"), reason="",
        )
        for i in range(5)
    ]
    v = o.check_target(basket[0], nlv=Decimal("10000"), proposed_basket=basket)
    assert v.ok is False
    assert "net" in v.reason


def test_target_check_passes_for_balanced_dollar_neutral_basket():
    o = _overlay(Decimal("10000"))
    longs = TargetPosition(
        symbol="AAPL", side=Side.LONG, target_qty=Decimal("5"),
        notional=Decimal("500"), reason="",
    )
    shorts = TargetPosition(
        symbol="MSFT", side=Side.SHORT, target_qty=Decimal("-5"),
        notional=Decimal("500"), reason="",
    )
    v = o.check_target(longs, nlv=Decimal("10000"), proposed_basket=[longs, shorts])
    assert v.ok is True
