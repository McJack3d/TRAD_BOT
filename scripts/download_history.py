"""Pull Binance OHLCV + funding history into Parquet partitions."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from src.adapters.binance import BinanceAdapter
from src.config import BotConfig, Secrets
from src.data.historical import HistoricalDownloader
from src.logging_setup import configure_logging, log


async def _run(config_path: str, start: str, end: str | None) -> None:
    cfg = BotConfig.from_yaml(config_path)
    secrets = Secrets()
    configure_logging(secrets.bot_log_level)

    start_dt = datetime.fromisoformat(start).replace(tzinfo=UTC)
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=UTC)
        if end
        else datetime.now(UTC)
    )

    exchange = BinanceAdapter(
        api_key=secrets.binance_api_key or "",
        api_secret=secrets.binance_api_secret or "",
        testnet=False,  # historical data is public on mainnet
    )
    await exchange.connect()
    downloader = HistoricalDownloader(exchange, data_dir=cfg.backtest.data_dir)

    try:
        for sc in cfg.symbols:
            log.info("history.download.symbol", symbol=sc.spot)
            await downloader.download_funding(sc.spot, start_dt, end_dt)
            await downloader.download_ohlcv(sc.spot, start_dt, end_dt)
    finally:
        await exchange.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Binance history")
    parser.add_argument("--config", default="config/backtest.yaml")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    args = parser.parse_args()
    asyncio.run(_run(args.config, args.start, args.end))


if __name__ == "__main__":
    main()
