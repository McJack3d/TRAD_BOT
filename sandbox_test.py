import sys
from decimal import Decimal
from datetime import datetime, timedelta

# Mock CheckResult
class CheckResult:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason
    @classmethod
    def pass_(cls):
        return cls(True)
    @classmethod
    def fail(cls, reason: str):
        return cls(False, reason)

# Mock _get_field helper from src/risk/perp_guards.py
def _get_field(obj, keys, default=None):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
    else:
        for k in keys:
            if hasattr(obj, k):
                return getattr(obj, k)
    return default

# Exact copy of check_asset_cooloff from src/risk/perp_guards.py
def check_asset_cooloff(
    symbol: str,
    closed_trades: list,
    current_bar_index: int,
    cooloff_bars: int = 6,
) -> CheckResult:
    symbol_trades = []
    for t in closed_trades:
        sym = _get_field(t, ["symbol", "asset"])
        if sym == symbol:
            symbol_trades.append(t)

    if not symbol_trades:
        return CheckResult.pass_()

    def sort_key(t) -> tuple[int, int | datetime | float]:
        bar_idx = _get_field(
            t,
            [
                "exit_bar_index",
                "closed_bar_index",
                "exit_index",
                "closed_at_index",
                "exit_bar",
                "bar_index",
            ],
        )
        if bar_idx is not None:
            return (0, int(bar_idx))
        ts = _get_field(t, ["exit_ts", "closed_at", "exit_time", "timestamp", "ts"])
        if ts is not None:
            return (1, ts)
        return (2, 0)

    symbol_trades = sorted(symbol_trades, key=sort_key)
    last_trade = symbol_trades[-1]

    pnl = _get_field(last_trade, ["net_pnl", "realized_pnl", "pnl"])
    if pnl is None:
        return CheckResult.pass_()

    pnl_dec = Decimal(str(pnl))
    if pnl_dec >= 0:
        return CheckResult.pass_()

    exit_bar = _get_field(
        last_trade,
        [
            "exit_bar_index",
            "closed_bar_index",
            "exit_index",
            "closed_at_index",
            "exit_bar",
            "bar_index",
        ],
    )
    if exit_bar is None:
        return CheckResult.pass_()

    elapsed = current_bar_index - int(exit_bar)
    if elapsed < cooloff_bars:
        return CheckResult.fail(
            f"Asset {symbol} is in cool-off: {elapsed} bars elapsed since last loss at bar {exit_bar} (cool-off: {cooloff_bars} bars)"
        )

    return CheckResult.pass_()

# Exact copy of check_consecutive_losses from src/risk/perp_guards.py
def check_consecutive_losses(
    closed_trades: list,
    max_consecutive_losses: int = 4,
) -> CheckResult:
    if len(closed_trades) < max_consecutive_losses:
        return CheckResult.pass_()

    def sort_key(t) -> tuple[int, int | datetime | float]:
        bar_idx = _get_field(
            t,
            [
                "exit_bar_index",
                "closed_bar_index",
                "exit_index",
                "closed_at_index",
                "exit_bar",
                "bar_index",
            ],
        )
        if bar_idx is not None:
            return (0, int(bar_idx))
        ts = _get_field(t, ["exit_ts", "closed_at", "exit_time", "timestamp", "ts"])
        if ts is not None:
            return (1, ts)
        return (2, 0)

    sorted_trades = sorted(closed_trades, key=sort_key)
    recent_trades = sorted_trades[-max_consecutive_losses:]

    losses = []
    for t in recent_trades:
        pnl = _get_field(t, ["net_pnl", "realized_pnl", "pnl"])
        if pnl is not None and Decimal(str(pnl)) < 0:
            losses.append(True)
        else:
            losses.append(False)

    if all(losses):
        return CheckResult.fail(
            f"Consecutive losses limit reached: last {max_consecutive_losses} trades were all losses."
        )

    return CheckResult.pass_()

def test_consecutive_losses_sorting_bug():
    print("--- Testing Bug 1: consecutive losses sorting across different symbols ---")
    # Scenario: 4 trades.
    # Trade 1: BTCUSDT, closed today 22:00. bar_index = 9990. PnL = -50 (Loss)
    # Trade 2: ETHUSDT, closed today 23:00 (later). bar_index = 495. PnL = +100 (Profit)
    # Trade 3: ETHUSDT, closed today 23:15. bar_index = 496. PnL = -20 (Loss)
    # Trade 4: ETHUSDT, closed today 23:30. bar_index = 497. PnL = -10 (Loss)
    
    # Chronologically, the order of closes is:
    # 1. BTCUSDT (Loss) @ 22:00
    # 2. ETHUSDT (Profit) @ 23:00
    # 3. ETHUSDT (Loss) @ 23:15
    # 4. ETHUSDT (Loss) @ 23:30
    # The last 4 trades are: Loss, Profit, Loss, Loss. (Only 3 losses, 1 profit, so consecutive losses breaker should NOT trigger)
    #
    # However, because it sorts by bar_index (9990 vs 495-497), the sorted trades are:
    # 1. ETHUSDT (Profit) @ 23:00 (bar 495)
    # 2. ETHUSDT (Loss) @ 23:15 (bar 496)
    # 3. ETHUSDT (Loss) @ 23:30 (bar 497)
    # 4. BTCUSDT (Loss) @ 22:00 (bar 9990)
    # Sorting puts BTCUSDT (Loss) @ 22:00 as the MOST RECENT trade!
    # The last 3 trades in the sorted list are: Loss, Loss, Loss.
    # If max_consecutive_losses is 3, the sorted list has last 3 trades as ALL losses.
    # The breaker will TRIGGER and halt all trading incorrectly!
    
    trades = [
        {"symbol": "BTCUSDT", "net_pnl": -50, "exit_bar_index": 9990, "exit_ts": datetime(2026, 6, 25, 22, 0)},
        {"symbol": "ETHUSDT", "net_pnl": 100, "exit_bar_index": 495, "exit_ts": datetime(2026, 6, 25, 23, 0)},
        {"symbol": "ETHUSDT", "net_pnl": -20, "exit_bar_index": 496, "exit_ts": datetime(2026, 6, 25, 23, 15)},
        {"symbol": "ETHUSDT", "net_pnl": -10, "exit_bar_index": 497, "exit_ts": datetime(2026, 6, 25, 23, 30)},
    ]
    
    res = check_consecutive_losses(trades, max_consecutive_losses=3)
    print(f"Breaker check with N=3: ok={res.ok}, reason='{res.reason}'")
    if not res.ok:
        print("[SUCCESS] Bug 1 reproduced: Consecutive losses triggered incorrect halt due to bar index sorting mixing symbols.")
    else:
        print("[FAILURE] Bug 1 not reproduced.")

def test_cooloff_bypass_bug():
    print("\n--- Testing Bug 2: cool-off bypass due to metadata propagation ---")
    # Scenario: 
    # 1. Stop loss hit on BTCUSDT at bar 100. PnL = -50.
    # System metadata saves: f"BTCUSDT_last_loss_exit_bar" = "100".
    #
    # 2. A new trade on BTCUSDT is opened and closed normally (e.g. EMA flip) at bar 110 at a loss (PnL = -20).
    # Since it is a normal exit, last_loss_exit_bar metadata is NOT updated. It remains "100".
    #
    # 3. We check cool-off at bar 112.
    # The code builds closed_trades list. For the recent trade (closed at 110), it does:
    #   exit_bar_str = meta.get("BTCUSDT_last_loss_exit_bar", None) # -> "100"
    #   exit_idx = int(exit_bar_str) # -> 100
    # So the trade that actually exited at bar 110 is recorded as exiting at bar 100!
    #
    # 4. check_asset_cooloff checks the last trade (which is the loss at bar 110, but exit_bar_index=100).
    # It calculates elapsed = current_bar_index - exit_bar = 112 - 100 = 12.
    # Since elapsed (12) >= cooloff_bars (6), the check PASSES!
    # In reality, only 2 bars have elapsed since the last loss (112 - 110 = 2), so cool-off should FAIL!
    
    meta = {
        "BTCUSDT_last_loss_exit_bar": "100"
    }
    
    # Simulating the closed_trades construction in regime_live.py (L453-470)
    closed_positions = [
        {"symbol": "BTCUSDT", "realized_pnl": -50, "closed_at": datetime(2026, 6, 25, 10, 0)}, # old stop hit
        {"symbol": "BTCUSDT", "realized_pnl": -20, "closed_at": datetime(2026, 6, 25, 11, 0)}, # new EMA flip loss
    ]
    
    closed_trades_reconstructed = []
    for p in closed_positions:
        exit_idx = None
        exit_bar_str = meta.get(f"{p['symbol']}_last_loss_exit_bar", None)
        if exit_bar_str:
            exit_idx = int(exit_bar_str)
        else:
            # Fallback (would normally look up from df, let's say it's 110)
            exit_idx = 110
        
        closed_trades_reconstructed.append({
            "symbol": p["symbol"],
            "net_pnl": p["realized_pnl"],
            "exit_bar_index": exit_idx,
        })
        
    res = check_asset_cooloff("BTCUSDT", closed_trades_reconstructed, current_bar_index=112, cooloff_bars=6)
    print(f"Cool-off check at bar 112: ok={res.ok}, reason='{res.reason}'")
    if res.ok:
        print("[SUCCESS] Bug 2 reproduced: Cool-off bypassed at bar 112 (only 2 bars elapsed since actual loss at bar 110).")
    else:
        print("[FAILURE] Bug 2 not reproduced.")

if __name__ == "__main__":
    test_consecutive_losses_sorting_bug()
    test_cooloff_bypass_bug()
