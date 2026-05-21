"""PaperBinanceAdapter — balance-seeding behavior."""

from __future__ import annotations

from decimal import Decimal


def test_paper_adapter_spot_only_seeds_all_in_spot() -> None:
    """Trend bot uses spot only. The paper adapter should put 100% of the
    starting balance under spot, not split 50/50 with perp."""
    from src.adapters.paper_binance import PaperBinanceAdapter

    ex = PaperBinanceAdapter(starting_usdt=Decimal("1000"), spot_only=True)
    bals = ex._balances
    assert bals.get("spot:USDT").total == Decimal("1000")
    assert "perp:USDT" not in bals


def test_paper_adapter_dual_seeds_split_50_50() -> None:
    """For funding-arb-style use cases, the 50/50 spot/perp split stays
    available via spot_only=False."""
    from src.adapters.paper_binance import PaperBinanceAdapter

    ex = PaperBinanceAdapter(starting_usdt=Decimal("1000"), spot_only=False)
    bals = ex._balances
    assert bals.get("spot:USDT").total == Decimal("500")
    assert bals.get("perp:USDT").total == Decimal("500")


def test_paper_adapter_custom_quote_currency() -> None:
    """USDC, EUR, etc. should seed under the right key."""
    from src.adapters.paper_binance import PaperBinanceAdapter

    ex = PaperBinanceAdapter(
        starting_usdt=Decimal("500"), quote_asset="USDC", spot_only=True
    )
    bals = ex._balances
    assert bals.get("spot:USDC").total == Decimal("500")
    assert "spot:USDT" not in bals
