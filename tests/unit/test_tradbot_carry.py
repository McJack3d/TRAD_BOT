"""Tests for the carry-backtest CLI wiring.

Mirrors the regime CLI tests: the subparser/handler contract, the menu
descriptor shape, and — most importantly — an end-to-end run of the
command through the data-loader hooks (no network), proving the async
chain executes cleanly and the acceptance gates render.
"""

from __future__ import annotations

import argparse
import io

import pandas as pd
import pytest
from rich.console import Console

from scripts import tradbot_carry
from src.data import history as histmod


def test_subparsers_match_handlers():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    tradbot_carry.register_subparsers(sub)
    for name in tradbot_carry.HANDLERS:
        assert name in sub.choices
    assert "carry-backtest" in sub.choices


def test_menu_items_well_formed():
    items = tradbot_carry.menu_items()
    assert len(items) == 1
    for key, label, fn, ns in items:
        assert isinstance(key, str)
        assert isinstance(label, str) and label
        assert callable(fn)
        assert hasattr(ns, "__dict__")
        # The namespace must carry every attr the handler reads.
        for attr in ("symbols", "months", "equity", "fee_bps", "slippage_bps", "refresh"):
            assert hasattr(ns, attr), attr


def _grid(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2022-01-01", periods=n, freq="8h", tz="UTC")


@pytest.mark.asyncio
async def test_carry_backtest_runs_end_to_end_via_hooks(tmp_path, monkeypatch):
    """REGRESSION/contract: the menu handler awaits the async loaders and
    feeds them to the backtester. Stub the network fetchers so the whole
    chain runs offline and returns a clean exit code."""
    monkeypatch.chdir(tmp_path)

    # Oscillating negative funding so the negative leg actually trades.
    def _osc(cycles=22, hi_len=8, lo_len=3, hi=-0.0008, lo=-0.00001):
        vals = []
        for _ in range(cycles):
            vals += [hi] * hi_len + [lo] * lo_len
        return vals

    async def fake_funding(symbol, since_ms, until_ms):
        vals = _osc()
        step = 8 * 3_600_000
        return [(int(since_ms) + i * step, v) for i, v in enumerate(vals)]

    async def fake_borrow(asset, since_ms, until_ms):
        # Cheap borrow (5% APR) for ~enough daily points to cover the span.
        step = 86_400_000
        return [(int(since_ms) + i * step, 0.05) for i in range(120)]

    monkeypatch.setattr(histmod, "_FUNDING_FETCHER", fake_funding)
    monkeypatch.setattr(histmod, "_BORROW_RATE_FETCHER", fake_borrow)

    console = Console(file=io.StringIO())
    ns = argparse.Namespace(
        symbols=["BTC/USDT"], months=2, equity=1000.0,
        fee_bps=4.0, slippage_bps=2.0, refresh=False,
    )
    rc = await tradbot_carry.cmd_carry_backtest(ns, console)
    # rc is 0 (all gates pass) or 2 (gates failed) — both are clean,
    # non-error exits. The point is it ran the full chain without raising.
    assert rc in (0, 2)
    out = console.file.getvalue()
    assert "Funding carry — BTC/USDT" in out
    assert "negative" in out
    assert "PASS" in out or "FAIL" in out


@pytest.mark.asyncio
async def test_carry_backtest_reports_clear_error_on_fetch_failure(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    async def boom(*a, **kw):
        raise RuntimeError("binance ExchangeNotAvailable GET exchangeInfo")

    monkeypatch.setattr(histmod, "_FUNDING_FETCHER", boom)
    console = Console(file=io.StringIO())
    ns = argparse.Namespace(
        symbols=["BTC/USDT"], months=1, equity=1000.0,
        fee_bps=4.0, slippage_bps=2.0, refresh=False,
    )
    rc = await tradbot_carry.cmd_carry_backtest(ns, console)
    assert rc == 1
    assert "Binance" in console.file.getvalue()
