"""Order id generation and rounding helpers."""

from __future__ import annotations

import uuid
from decimal import ROUND_DOWN, Decimal


def generate_client_order_id(prefix: str = "trad") -> str:
    """Generate a client-order id Binance will accept (<= 36 chars).

    Format: <prefix>-<uuid4 hex first 16 chars> → "trad-ab12cd34ef56gh78".
    """
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def round_qty(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def round_price(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick
