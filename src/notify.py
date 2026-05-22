"""Lightweight cross-platform notifier.

Default implementation: macOS `osascript` for banner notifications.
On Linux / Windows it's a no-op (the message still goes to logs).
Pluggable so tests pass a stub.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Protocol


class Notifier(Protocol):
    def __call__(self, title: str, message: str) -> None: ...


def macos_notifier(title: str, message: str) -> None:
    """Show a native macOS banner. Silent failure if osascript isn't
    available or the process doesn't have permission."""
    if sys.platform != "darwin":
        return
    # Escape any double quotes the message might contain.
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=5,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def noop_notifier(title: str, message: str) -> None:
    """Default — used in tests and on non-macOS systems."""
    return None


def best_notifier() -> Notifier:
    """Pick the right notifier for the current platform."""
    if sys.platform == "darwin":
        return macos_notifier
    return noop_notifier
