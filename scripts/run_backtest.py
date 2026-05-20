"""Run a backtest from YAML config and print metrics."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from rich.console import Console
from rich.table import Table

from src.backtest.engine import BacktestEngine
from src.backtest.metrics import compute_metrics
from src.backtest.walk_forward import walk_forward
from src.config import BotConfig
from src.logging_setup import configure_logging


def _run(config_path: str, walk: bool) -> None:
    configure_logging("INFO")
    cfg = BotConfig.from_yaml(config_path)
    start = datetime.fromisoformat(cfg.backtest.start).replace(tzinfo=UTC)
    end = (
        datetime.fromisoformat(cfg.backtest.end).replace(tzinfo=UTC)
        if cfg.backtest.end
        else datetime.now(UTC)
    )
    console = Console()

    if walk:
        windows = walk_forward(cfg, start, end, data_dir=cfg.backtest.data_dir)
        table = Table(title="Walk-forward results")
        table.add_column("train_start")
        table.add_column("test_start")
        table.add_column("entry")
        table.add_column("exit")
        table.add_column("train Sharpe")
        table.add_column("test Sharpe")
        table.add_column("test APR")
        table.add_column("test maxDD")
        for w in windows:
            table.add_row(
                w.train_start.date().isoformat(),
                w.test_start.date().isoformat(),
                str(w.best_entry_threshold),
                str(w.best_exit_threshold),
                f"{w.train_metrics.sharpe:.2f}",
                f"{w.test_metrics.sharpe:.2f}",
                f"{w.test_metrics.net_apr:.2%}",
                f"{w.test_metrics.max_drawdown:.2%}",
            )
        console.print(table)
        return

    engine = BacktestEngine(cfg, data_dir=cfg.backtest.data_dir)
    result = engine.run(start, end, cfg.backtest.initial_equity_eur)
    m = compute_metrics(result)

    table = Table(title="Backtest summary")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("Initial equity", f"{m.initial_equity:.2f}")
    table.add_row("Final equity", f"{m.final_equity:.2f}")
    table.add_row("Net APR", f"{m.net_apr:.2%}")
    table.add_row("Sharpe", f"{m.sharpe:.2f}")
    table.add_row("Max drawdown", f"{m.max_drawdown:.2%}")
    table.add_row("Time in market", f"{m.time_in_market_pct:.2%}")
    table.add_row("Avg dwell (h)", f"{m.avg_dwell_hours:.1f}")
    table.add_row("Trades", str(m.n_trades))
    table.add_row("Win rate", f"{m.win_rate:.2%}")
    table.add_row("Avg PnL / trade", f"{m.avg_pnl_per_trade:.4f}")
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--walk-forward", action="store_true")
    args = parser.parse_args()
    _run(args.config, walk=args.walk_forward)


if __name__ == "__main__":
    main()
