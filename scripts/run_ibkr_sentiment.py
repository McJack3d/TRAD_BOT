"""Thin wrapper so the IBKR sentiment bot can be launched as a script.

Usage:
    python -m scripts.run_ibkr_sentiment --config config/ibkr_sentiment.yaml
    ibkr-sentiment-bot --config config/ibkr_sentiment.yaml
"""

from __future__ import annotations

from src.ibkr_sentiment.main import cli


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
