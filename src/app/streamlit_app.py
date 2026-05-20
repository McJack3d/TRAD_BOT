"""Streamlit UI for the SimpleBot trend follower.

Run with:
    streamlit run src/app/streamlit_app.py

Closing the terminal kills the bot. Closing the browser tab does not —
the Streamlit server keeps running and will continue to evaluate on
the next page load. To fully stop, Ctrl+C the terminal.

Modes:
- Paper (default): uses FakeExchange with seeded balance, safe.
- Live: reads BINANCE_API_KEY / BINANCE_API_SECRET from env, uses
  Binance mainnet. Big red banner.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pandas as pd
import streamlit as st

from src.adapters.fake import FakeExchange
from src.simple_bot import SimpleBot
from src.state import Database
from src.strategy.sma_trend import TrendState

DB_PATH = os.environ.get("SIMPLE_BOT_DB", "data/simple_bot.db")
LIVE = os.environ.get("SIMPLE_BOT_LIVE", "false").lower() == "true"
STARTING_USDT = Decimal(os.environ.get("SIMPLE_BOT_STARTING_USDT", "1000"))
SMA_WINDOW = int(os.environ.get("SIMPLE_BOT_SMA_WINDOW", "50"))
SYMBOL = os.environ.get("SIMPLE_BOT_SYMBOL", "BTC/USDT")


def _run(coro):
    """Wrap async calls so Streamlit handlers can use them."""
    return asyncio.run(coro)


@st.cache_resource
def _resources():
    db = Database(DB_PATH)
    _run(db.init(starting_equity=STARTING_USDT))
    if LIVE:
        from src.adapters.binance import BinanceAdapter

        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        testnet = os.environ.get("BINANCE_TESTNET", "false").lower() == "true"
        if not (api_key and api_secret):
            st.error("LIVE mode requires BINANCE_API_KEY and BINANCE_API_SECRET")
            st.stop()
        ex = BinanceAdapter(api_key=api_key, api_secret=api_secret, testnet=testnet)
        _run(ex.connect())
    else:
        ex = FakeExchange(starting_usdt=STARTING_USDT)
        # Seed a plausible ticker so the UI shows real-looking numbers.
        ex.set_ticker(SYMBOL, "spot", Decimal("60000"))
    bot = SimpleBot(exchange=ex, db=db, symbol=SYMBOL, sma_window=SMA_WINDOW)
    return bot, ex, db


def main() -> None:
    st.set_page_config(page_title="Trend bot", layout="centered")
    st.title("BTC trend bot")

    if LIVE:
        st.error("🚨 LIVE MODE — real money on Binance mainnet 🚨")
    else:
        st.info("Paper mode (FakeExchange). Set SIMPLE_BOT_LIVE=true to trade real funds.")

    bot, exchange, db = _resources()
    status = _run(bot.status())

    col1, col2, col3 = st.columns(3)
    col1.metric("Trading", "ON" if status.enabled else "OFF")
    col2.metric("Position", status.current_state.value.upper())
    col3.metric("Last BTC price", f"{status.last_price:.2f}" if status.last_price else "—")

    st.subheader("Holdings")
    st.write(
        pd.DataFrame(
            [
                {"asset": "BTC", "qty": float(status.btc_qty)},
                {"asset": "USDT", "qty": float(status.usdt_qty)},
            ]
        )
    )

    if status.last_signal is not None:
        st.caption(
            f"Last signal: {status.last_signal.state.value} "
            f"(close={status.last_signal.close}, SMA={status.last_signal.sma}). "
            f"Reason: {status.last_signal.reason}"
        )
    if status.last_evaluated is not None:
        st.caption(f"Last evaluated: {status.last_evaluated.isoformat()}")

    st.divider()
    st.subheader("Controls")

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("▶ Start trading", type="primary", use_container_width=True):
        _run(bot.enable())
        st.rerun()
    if c2.button("⏸ Stop trading", use_container_width=True):
        _run(bot.disable())
        st.rerun()
    if c3.button("🔄 Evaluate now", use_container_width=True):
        sig = _run(bot.tick())
        if sig is None:
            st.warning("Bot is disabled — click Start trading first.")
        else:
            st.success(f"Signal: {sig.state.value} ({sig.reason})")
        st.rerun()
    if c4.button("💵 Flatten to USDT", use_container_width=True):
        _run(bot.flatten_now())
        st.rerun()

    st.divider()
    with st.expander("Strategy details"):
        st.markdown(
            f"""
            **Asset:** {SYMBOL}
            **Rule:** Close > {SMA_WINDOW}-day SMA → hold BTC; otherwise hold USDT.
            **Evaluation:** triggered on every page load and by clicking *Evaluate now*.
            **No leverage. No shorting. No funding settlements.**

            Closing the browser tab does NOT stop the bot — the Streamlit
            server in your terminal is still running. To fully stop:
            Ctrl+C the terminal.
            """
        )


if __name__ == "__main__":
    main()
