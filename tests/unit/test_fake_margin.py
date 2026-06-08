"""FakeExchange cross-margin behaviour — the negative-leg primitives.

Deterministic, no network. These lock the harness the two-sided carry
backtester and paper engine lean on: borrow/repay accounting, interest
accrual, the margin-level ratio the risk overlay gates on, and the
no-inventory rejection that forces an atomic abort of the two-leg open.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.adapters.exchange_base import BorrowInfo, MarginAccount
from src.adapters.fake import FakeExchange


def _ex() -> FakeExchange:
    return FakeExchange(starting_usdt=Decimal("1000"))


# ---- borrow ----------------------------------------------------------


async def test_borrow_without_rate_raises_simulating_no_inventory():
    """No rate set ⇒ no inventory. The execution engine pre-flights borrow
    and must see a hard failure so it can abort the perp leg cleanly."""
    ex = _ex()
    with pytest.raises(RuntimeError, match="no borrow rate"):
        await ex.borrow("BTC", Decimal("0.5"))


async def test_borrow_rejects_non_positive_amount():
    ex = _ex()
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    with pytest.raises(ValueError):
        await ex.borrow("BTC", Decimal("0"))
    with pytest.raises(ValueError):
        await ex.borrow("BTC", Decimal("-1"))


async def test_borrow_records_principal_and_credits_margin_balance():
    ex = _ex()
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    await ex.borrow("BTC", Decimal("0.5"))
    info = await ex.fetch_borrow_info("BTC")
    assert info.borrowed == Decimal("0.5")
    assert info.interest_accrued == Decimal("0")
    assert info.borrow_rate_apr == Decimal("0.10")
    # Borrowed asset lands in the margin account, ready to be sold short.
    acct = await ex.fetch_margin_account()
    assert acct.balances["BTC"].total == Decimal("0.5")


async def test_borrow_accumulates_across_calls():
    ex = _ex()
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    await ex.borrow("BTC", Decimal("0.5"))
    await ex.borrow("BTC", Decimal("0.25"))
    info = await ex.fetch_borrow_info("BTC")
    assert info.borrowed == Decimal("0.75")


# ---- interest accrual ------------------------------------------------


async def test_interest_accrues_at_apr_over_one_year():
    """1 BTC @ 10% APR over exactly 8760h (one year) accrues 0.10 BTC."""
    ex = _ex()
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    await ex.borrow("BTC", Decimal("1"))
    ex.accrue_borrow_interest(hours=8760.0)
    info = await ex.fetch_borrow_info("BTC")
    assert info.interest_accrued == Decimal("0.10")


async def test_interest_accrues_proportionally_per_8h():
    """One 8h settlement: 1 BTC @ 10.95% APR accrues 0.10/1095 ≈ 0.0001."""
    ex = _ex()
    ex.set_borrow_rate("BTC", Decimal("0.1095"))
    await ex.borrow("BTC", Decimal("1"))
    ex.accrue_borrow_interest(hours=8.0)
    info = await ex.fetch_borrow_info("BTC")
    expected = Decimal("1") * Decimal("0.1095") * (Decimal("8") / Decimal("8760"))
    assert info.interest_accrued == expected


# ---- repay -----------------------------------------------------------


async def test_repay_applies_to_interest_first_then_principal():
    ex = _ex()
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    await ex.borrow("BTC", Decimal("1"))
    ex.accrue_borrow_interest(hours=8760.0)  # 0.10 BTC interest
    # Repay 0.04: all goes to interest, principal untouched.
    await ex.repay("BTC", Decimal("0.04"))
    info = await ex.fetch_borrow_info("BTC")
    assert info.interest_accrued == Decimal("0.06")
    assert info.borrowed == Decimal("1")


async def test_repay_beyond_interest_reduces_principal():
    ex = _ex()
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    await ex.borrow("BTC", Decimal("1"))
    ex.accrue_borrow_interest(hours=8760.0)  # 0.10 interest
    # Repay 0.30: 0.10 clears interest, 0.20 hits principal.
    await ex.repay("BTC", Decimal("0.30"))
    info = await ex.fetch_borrow_info("BTC")
    assert info.interest_accrued == Decimal("0")
    assert info.borrowed == Decimal("0.80")


async def test_repay_never_drives_principal_negative():
    ex = _ex()
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    await ex.borrow("BTC", Decimal("1"))
    await ex.repay("BTC", Decimal("5"))  # way over
    info = await ex.fetch_borrow_info("BTC")
    assert info.borrowed == Decimal("0")
    assert info.interest_accrued == Decimal("0")


async def test_repay_rejects_non_positive_amount():
    ex = _ex()
    with pytest.raises(ValueError):
        await ex.repay("BTC", Decimal("0"))


# ---- margin account / margin_level -----------------------------------


async def test_margin_level_is_infinite_sentinel_with_no_debt():
    ex = _ex()
    acct = await ex.fetch_margin_account()
    assert isinstance(acct, MarginAccount)
    assert acct.total_liability_value == Decimal("0")
    assert acct.margin_level == Decimal("9999")


async def test_margin_level_reflects_assets_over_liabilities():
    """Borrow 1 BTC @ $100 with $100 of margin assets ⇒ assets and
    liabilities are both ~$100, so margin_level ≈ 1 (before the spot is
    sold). The ratio is what the risk overlay gates on at 2.0."""
    ex = _ex()
    ex.set_ticker("BTC/USDT", "spot", Decimal("100"))
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    # Seed some USDT margin collateral so assets exceed the borrow.
    ex.set_margin_balance("USDT", Decimal("100"))
    await ex.borrow("BTC", Decimal("1"))  # +1 BTC asset, +1 BTC liability
    acct = await ex.fetch_margin_account()
    # Assets: 100 USDT + 1 BTC*100 = 200. Liability: 1 BTC*100 = 100.
    assert acct.total_asset_value == Decimal("200")
    assert acct.total_liability_value == Decimal("100")
    assert acct.margin_level == Decimal("2")


async def test_margin_level_drops_as_interest_accrues():
    ex = _ex()
    ex.set_ticker("BTC/USDT", "spot", Decimal("100"))
    ex.set_borrow_rate("BTC", Decimal("0.10"))
    ex.set_margin_balance("USDT", Decimal("100"))
    await ex.borrow("BTC", Decimal("1"))
    before = (await ex.fetch_margin_account()).margin_level
    ex.accrue_borrow_interest(hours=8760.0)  # +0.10 BTC liability
    after = (await ex.fetch_margin_account()).margin_level
    assert after < before


async def test_fetch_borrow_info_unknown_asset_is_zeroed():
    ex = _ex()
    info = await ex.fetch_borrow_info("ETH")
    assert isinstance(info, BorrowInfo)
    assert info.borrowed == Decimal("0")
    assert info.interest_accrued == Decimal("0")
