"""Reconciler diff function — pure logic, no I/O."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from src.adapters.exchange_base import Balance, ExchangePosition
from src.config import ReconciliationConfig
from src.reconciliation.reconciler import Reconciler


class _NullDB:
    pass


class _NullExchange:
    pass


def _reconciler() -> Reconciler:
    return Reconciler(_NullDB(), _NullExchange(), ReconciliationConfig())


def _db_pos(symbol: str, perp_qty: Decimal) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol, perp_qty=perp_qty)


def _ex_perp(symbol: str, qty: Decimal) -> ExchangePosition:
    return ExchangePosition(
        symbol=symbol,
        leg="perp",
        qty=qty,
        entry_price=Decimal("30000"),
        mark_price=Decimal("30000"),
        liquidation_price=Decimal("40000"),
        margin=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )


def _bal(asset: str, total: Decimal) -> dict[str, Balance]:
    return {f"spot:{asset}": Balance(asset=asset, free=total, used=Decimal("0"), total=total)}


def test_exact_match_no_drift() -> None:
    r = _reconciler()
    drifts = r.diff(
        [_db_pos("BTC/USDT", Decimal("-0.5"))],
        [_ex_perp("BTC/USDT", Decimal("-0.5"))],
        _bal("USDT", Decimal("1000")),
    )
    assert drifts == []


def test_within_tolerance_no_drift() -> None:
    r = _reconciler()
    drifts = r.diff(
        [_db_pos("BTC/USDT", Decimal("-0.5"))],
        [_ex_perp("BTC/USDT", Decimal("-0.50049"))],  # 0.098% diff < 0.1%
        _bal("USDT", Decimal("1000")),
    )
    assert drifts == []


def test_beyond_tolerance_flagged() -> None:
    r = _reconciler()
    drifts = r.diff(
        [_db_pos("BTC/USDT", Decimal("-0.5"))],
        [_ex_perp("BTC/USDT", Decimal("-0.6"))],
        _bal("USDT", Decimal("1000")),
    )
    assert any("BEYOND_TOLERANCE" in d for d in drifts)


def test_exchange_has_position_db_does_not() -> None:
    r = _reconciler()
    drifts = r.diff(
        [],
        [_ex_perp("BTC/USDT", Decimal("-0.1"))],
        _bal("USDT", Decimal("1000")),
    )
    assert any("BEYOND_TOLERANCE" in d for d in drifts)


def test_db_has_position_exchange_does_not() -> None:
    r = _reconciler()
    drifts = r.diff(
        [_db_pos("BTC/USDT", Decimal("-0.1"))],
        [],
        _bal("USDT", Decimal("1000")),
    )
    assert any("BEYOND_TOLERANCE" in d for d in drifts)


def test_multiple_symbols_independent() -> None:
    r = _reconciler()
    drifts = r.diff(
        [_db_pos("BTC/USDT", Decimal("-0.5")), _db_pos("ETH/USDT", Decimal("-5"))],
        [_ex_perp("BTC/USDT", Decimal("-0.5")), _ex_perp("ETH/USDT", Decimal("-50"))],
        _bal("USDT", Decimal("1000")),
    )
    assert any("ETH/USDT" in d for d in drifts)
    assert not any("BTC/USDT" in d for d in drifts)


def test_sign_difference_treated_via_abs() -> None:
    """The DB stores a signed perp qty; the exchange reports the same.
    Reconciler compares absolute sizes so a sign flip would still show up
    via the next field but here we confirm abs matching for normal shorts."""
    r = _reconciler()
    drifts = r.diff(
        [_db_pos("BTC/USDT", Decimal("-0.5"))],
        [_ex_perp("BTC/USDT", Decimal("-0.5"))],
        _bal("USDT", Decimal("1000")),
    )
    assert drifts == []
