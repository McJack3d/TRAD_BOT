"""Tests for the regime-backtest CLI wiring."""

from __future__ import annotations

import argparse
import io

import pytest
from rich.console import Console

from scripts import tradbot_regime
from src.data import history as histmod


def test_subparsers_match_handlers():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    tradbot_regime.register_subparsers(sub)
    for name in tradbot_regime.HANDLERS:
        assert name in sub.choices
    for name in sub.choices:
        if name.startswith("regime-"):
            assert name in tradbot_regime.HANDLERS


def test_menu_items_well_formed():
    items = tradbot_regime.menu_items()
    assert len(items) == 3
    keys = [k for k, _, _, _ in items]
    assert len(keys) == len(set(keys))
    for key, label, fn, ns in items:
        assert isinstance(key, str)
        assert isinstance(label, str) and label
        assert callable(fn)
        assert hasattr(ns, "__dict__")


def test_build_args_defaults_sensible():
    ns = tradbot_regime._build_args(
        "BTC/USDT,ETH/USDT", "1h", months=3, sweep=False
    )
    assert ns.symbols == ["BTC/USDT", "ETH/USDT"]
    assert ns.timeframes == ["1h"]
    assert ns.months == 3
    # Sizing policy from the spec — must not drift silently.
    assert ns.risk_pct == 0.01
    assert ns.max_leverage == 3.0
    assert ns.fee_bps == 4.0
    assert ns.slippage_bps == 2.0
    # The handlers must pass a `debug` attr through to the backtest CLI.
    assert hasattr(ns, "debug")


@pytest.mark.asyncio
async def test_regime_quick_runs_end_to_end_without_asyncio_nesting(
    tmp_path, monkeypatch
):
    """REGRESSION: the menu handler awaits the async backtest chain. The
    v1 code called a SYNC data loader (which did asyncio.run internally)
    from inside this already-running loop, crashing with 'asyncio.run()
    cannot be called from a running event loop' and mislabelling it as a
    Binance geo-block. This proves the whole chain runs cleanly."""
    # Keep the parquet cache inside tmp by running from there.
    monkeypatch.chdir(tmp_path)

    async def fake_ohlcv(symbol, timeframe, since_ms, until_ms):
        # A clean uptrend, enough bars for the backtest to run.
        base, step = 1_700_000_000_000, 3_600_000
        rows = []
        price = 100.0
        for i in range(700):
            price += 0.2
            rows.append([base + i * step, price, price + 0.3, price - 0.3, price, 1000.0])
        return rows

    monkeypatch.setattr(histmod, "_OHLCV_FETCHER", fake_ohlcv)

    console = Console(file=io.StringIO())
    rc = await tradbot_regime.cmd_regime_quick(argparse.Namespace(), console)
    assert rc == 0  # ran end-to-end, produced a scorecard, no crash
