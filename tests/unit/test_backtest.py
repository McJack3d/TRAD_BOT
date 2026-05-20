"""Backtest engine smoke test on synthetic data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.backtest.engine import BacktestEngine
from src.backtest.metrics import compute_metrics


def _write_funding(data_dir: Path, symbol: str, n_events: int, rate: Decimal) -> None:
    folder = data_dir / "funding" / symbol.replace("/", "_")
    folder.mkdir(parents=True, exist_ok=True)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    rows = []
    for i in range(n_events):
        ts = start + timedelta(hours=8 * i)
        rows.append(
            {
                "ts": ts,
                "symbol": symbol,
                "funding_rate": float(rate),
                "mark_price": 30000.0,
            }
        )
    df = pd.DataFrame(rows)
    df.to_parquet(folder / "2024-01.parquet", index=False)


def test_backtest_smoke_positive_funding_makes_money(bot_cfg, tmp_path: Path) -> None:
    data_dir = tmp_path / "history"
    _write_funding(data_dir, "BTC/USDT", n_events=90, rate=Decimal("0.0003"))
    _write_funding(data_dir, "ETH/USDT", n_events=90, rate=Decimal("0.0003"))

    engine = BacktestEngine(bot_cfg, data_dir=str(data_dir))
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 31, tzinfo=UTC)
    result = engine.run(start, end, initial_equity=Decimal("1000"))

    metrics = compute_metrics(result)
    # Funding 0.03% × 3/day × 30 days × 50% capital ≈ +135 USDT before fees,
    # but per-event-cycle target re-evaluation and fees eat some. Still must
    # end well above initial when funding is persistently positive and large.
    assert result.final_equity > result.initial_equity
    assert metrics.n_trades >= 1


def test_backtest_no_data_returns_initial(bot_cfg, tmp_path: Path) -> None:
    engine = BacktestEngine(bot_cfg, data_dir=str(tmp_path / "empty"))
    result = engine.run(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 31, tzinfo=UTC),
        initial_equity=Decimal("1000"),
    )
    assert result.final_equity == Decimal("1000")
    assert result.trades == []
