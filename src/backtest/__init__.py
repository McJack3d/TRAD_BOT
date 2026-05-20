"""Backtesters.

Import the specific module you need rather than relying on this
package's __init__ — keeps the funding-arb chain (which pulls in
exchange adapters, market data, etc.) from being loaded when you only
want the SMA trend backtest, and vice versa.
"""
