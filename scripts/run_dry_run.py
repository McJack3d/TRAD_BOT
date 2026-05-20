"""Dry-run: full logic on the production VPS, but every order submission
is intercepted and logged instead of sent. Used during Phase 5
acceptance.
"""

from __future__ import annotations

import argparse
import asyncio

from src.config import BotConfig, Mode
from src.logging_setup import configure_logging, log
from src.main import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Run trad-bot in dry-run mode")
    parser.add_argument("--config", default="config/paper.yaml")
    parser.add_argument("--kill-file", default="/var/lib/bot/KILL")
    args = parser.parse_args()

    cfg = BotConfig.from_yaml(args.config)
    if cfg.mode == Mode.LIVE:
        raise SystemExit("refusing to dry-run a LIVE config; pass paper.yaml or backtest.yaml")

    configure_logging("INFO")
    log.info("dry_run.starting", config=args.config)
    asyncio.run(run(args.config, args.kill_file))


if __name__ == "__main__":
    main()
