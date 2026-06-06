"""Market data + history loaders.

Currently provides the OHLCV / funding history loader used by the
regime-switch backtester (`src.data.history`). The live market-data
stream modules (`market_data`, `binance_ws`) referenced by the
funding-arb daemon are not part of this package yet.
"""
