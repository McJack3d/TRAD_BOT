"""Order helper tests."""

from __future__ import annotations

from decimal import Decimal

from src.execution.order import generate_client_order_id, round_price, round_qty


def test_client_order_id_is_unique() -> None:
    ids = {generate_client_order_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_client_order_id_fits_binance_limit() -> None:
    assert all(len(generate_client_order_id()) <= 36 for _ in range(50))


def test_round_qty_floors_to_step() -> None:
    assert round_qty(Decimal("0.123456"), Decimal("0.00001")) == Decimal("0.12345")


def test_round_qty_zero_step_is_passthrough() -> None:
    assert round_qty(Decimal("1.234"), Decimal("0")) == Decimal("1.234")


def test_round_price_floors_to_tick() -> None:
    assert round_price(Decimal("30000.149"), Decimal("0.01")) == Decimal("30000.14")
