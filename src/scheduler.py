"""macOS launchd installer for daily auto-evaluation.

Writes a LaunchAgent plist that runs `tradbot evaluate` at a fixed UTC
hour every day, then loads it via `launchctl`. The agent survives
reboots, runs only while the user is logged in, and logs to
~/Library/Logs/tradbot/.

Doesn't try to be cross-platform — Linux/Windows users should use cron
/ Task Scheduler with the same `tradbot evaluate` command. This module
is macOS-only by design.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LABEL = "com.tradbot.daily"
PLIST_NAME = f"{LABEL}.plist"


@dataclass
class SchedulerPaths:
    plist: Path
    stdout_log: Path
    stderr_log: Path
    project_root: Path
    venv_python: Path
    tradbot_module: str = "scripts.tradbot"


def paths(project_root: Path) -> SchedulerPaths:
    home = Path.home()
    return SchedulerPaths(
        plist=home / "Library" / "LaunchAgents" / PLIST_NAME,
        stdout_log=home / "Library" / "Logs" / "tradbot" / "evaluate.log",
        stderr_log=home / "Library" / "Logs" / "tradbot" / "evaluate.err",
        project_root=project_root,
        venv_python=project_root / ".venv" / "bin" / "python",
    )


def build_plist(p: SchedulerPaths, hour_utc: int, minute_utc: int = 5) -> str:
    """Return the plist XML body. Hour/minute are UTC; launchd interprets
    `StartCalendarInterval` in LOCAL time, so we have to convert at install
    time using the user's current TZ offset."""
    from datetime import datetime, timezone

    # Convert the requested UTC time to local time.
    now_utc = datetime.now(timezone.utc).replace(hour=hour_utc, minute=minute_utc, second=0, microsecond=0)
    local = now_utc.astimezone()
    hour_local = local.hour
    minute_local = local.minute

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{p.venv_python}</string>
        <string>-m</string>
        <string>{p.tradbot_module}</string>
        <string>evaluate</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{p.project_root}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{p.venv_python.parent}:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{hour_local}</integer>
        <key>Minute</key>
        <integer>{minute_local}</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>{p.stdout_log}</string>

    <key>StandardErrorPath</key>
    <string>{p.stderr_log}</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def install(project_root: Path, hour_utc: int = 0, minute_utc: int = 5) -> SchedulerPaths:
    if sys.platform != "darwin":
        raise SystemExit(
            "Auto-scheduling via launchd is macOS-only. On Linux use cron:\n"
            f"  5 0 * * *  cd {project_root} && {project_root}/.venv/bin/python -m scripts.tradbot evaluate"
        )
    p = paths(project_root)
    if not p.venv_python.exists():
        raise SystemExit(
            f"Venv python not found at {p.venv_python}. Activate / install the venv first."
        )

    # Make sure log directory exists.
    p.stdout_log.parent.mkdir(parents=True, exist_ok=True)

    plist_body = build_plist(p, hour_utc=hour_utc, minute_utc=minute_utc)
    p.plist.parent.mkdir(parents=True, exist_ok=True)
    p.plist.write_text(plist_body)

    # If a previous version is loaded, unload it first to pick up changes.
    subprocess.run(
        ["launchctl", "unload", str(p.plist)],
        check=False,
        capture_output=True,
    )
    res = subprocess.run(
        ["launchctl", "load", str(p.plist)],
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise SystemExit(
            f"launchctl load failed: {res.stderr.strip() or res.stdout.strip()}\n"
            f"Plist at {p.plist} — you can inspect and load manually."
        )
    return p


def uninstall(project_root: Path) -> Path | None:
    """Remove the LaunchAgent. Returns the plist path that was removed,
    or None if there was nothing to remove."""
    if sys.platform != "darwin":
        return None
    p = paths(project_root)
    if not p.plist.exists():
        return None
    subprocess.run(
        ["launchctl", "unload", str(p.plist)],
        check=False,
        capture_output=True,
    )
    p.plist.unlink()
    return p.plist


def status(project_root: Path) -> dict:
    """Return a small dict describing the current install state."""
    if sys.platform != "darwin":
        return {"platform": sys.platform, "installed": False}
    p = paths(project_root)
    if not p.plist.exists():
        return {"platform": "darwin", "installed": False}
    res = subprocess.run(
        ["launchctl", "list", LABEL],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "platform": "darwin",
        "installed": True,
        "loaded": res.returncode == 0,
        "plist": str(p.plist),
        "stdout_log": str(p.stdout_log),
        "stderr_log": str(p.stderr_log),
        "label": LABEL,
    }
