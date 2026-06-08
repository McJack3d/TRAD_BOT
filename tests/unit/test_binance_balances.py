"""BinanceAdapter.fetch_balances resilience tests (no network).

Uses a tiny stub in place of ccxt clients so we can simulate the
common live failure mode: a spot-only API key whose futures-account
query is rejected.
"""

from __future__ import annotations

from decimal import Decimal

import pytest


class _FakeCcxt:
    """Minimal stand-in for a ccxt client."""

    def __init__(self, balance: dict | None = None, raises: Exception | None = None):
        self._balance = balance
        self._raises = raises

    async def fetch_balance(self) -> dict:
        if self._raises is not None:
            raise self._raises
        return self._balance


def _adapter_with(spot_client, perp_client):
    """Build a BinanceAdapter without running its ccxt-constructing __init__."""
    from src.adapters.binance import BinanceAdapter

    adapter = BinanceAdapter.__new__(BinanceAdapter)
    adapter.api_key = "test_api_key"
    adapter.api_secret = "test_api_secret"
    adapter.spot = spot_client
    adapter.perp = perp_client
    return adapter


async def test_spot_only_key_still_returns_spot_balances() -> None:
    """Futures query fails (no futures permission) → spot balances still
    come back. This is the trend-bot live scenario."""
    spot = _FakeCcxt(
        balance={
            "total": {"USDC": 21.92, "BTC": 0.0},
            "free": {"USDC": 21.92, "BTC": 0.0},
            "used": {"USDC": 0.0, "BTC": 0.0},
        }
    )
    perp = _FakeCcxt(raises=PermissionError("futures not enabled for this key"))
    adapter = _adapter_with(spot, perp)

    balances = await adapter.fetch_balances()
    assert "spot:USDC" in balances
    assert balances["spot:USDC"].total == Decimal("21.92")
    # Perp leg failed silently — no perp keys present.
    assert not any(k.startswith("perp:") for k in balances)


async def test_both_legs_failing_raises() -> None:
    spot = _FakeCcxt(raises=ConnectionError("spot down"))
    perp = _FakeCcxt(raises=ConnectionError("perp down"))
    adapter = _adapter_with(spot, perp)
    with pytest.raises(RuntimeError, match="all balance legs failed"):
        await adapter.fetch_balances()


async def test_both_legs_ok_merges() -> None:
    spot = _FakeCcxt(
        balance={"total": {"USDC": 10.0}, "free": {"USDC": 10.0}, "used": {"USDC": 0.0}}
    )
    perp = _FakeCcxt(
        balance={"total": {"USDC": 5.0}, "free": {"USDC": 5.0}, "used": {"USDC": 0.0}}
    )
    adapter = _adapter_with(spot, perp)
    balances = await adapter.fetch_balances()
    assert balances["spot:USDC"].total == Decimal("10.0")
    assert balances["perp:USDC"].total == Decimal("5.0")
