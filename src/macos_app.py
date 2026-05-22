"""Build a double-clickable macOS .app launcher for the bot.

The .app is a thin launcher, NOT a frozen Python bundle: double-clicking
it opens Terminal and runs `tradbot menu` (the interactive menu) from
the project's virtualenv. This keeps the app tiny (a few KB) and means
it always runs the current code — no rebuild needed after `git pull`.

macOS-only. The project root is baked into the launcher at build time.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

APP_NAME = "TradBot"
BUNDLE_ID = "com.tradbot.app"


def info_plist() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>{APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>{APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>{BUNDLE_ID}</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>{APP_NAME}</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
</dict>
</plist>
"""


def launcher_script(project_root: Path) -> str:
    """The bundle's executable. On double-click macOS runs this; it asks
    Terminal to open and run the interactive menu inside the venv."""
    venv_python = project_root / ".venv" / "bin" / "python"
    # The inner command run inside Terminal. Single-quoted path is safe
    # because project paths under ~ don't normally contain single quotes.
    inner = (
        f"cd '{project_root}' && "
        f"'{venv_python}' -m scripts.tradbot menu"
    )
    return f"""#!/bin/bash
# {APP_NAME} launcher — opens Terminal and runs the interactive bot menu.
/usr/bin/osascript <<'APPLESCRIPT'
tell application "Terminal"
    activate
    do script "{inner}"
end tell
APPLESCRIPT
"""


def build_app(project_root: Path, dest_dir: Path | None = None) -> Path:
    """Create `<dest_dir>/TradBot.app`. Returns the bundle path.

    Default dest_dir is ~/Applications (no admin rights needed).
    """
    if sys.platform != "darwin":
        raise SystemExit(
            "The .app launcher is macOS-only. On other systems just use "
            "`python -m scripts.tradbot menu` directly."
        )
    venv_python = project_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        raise SystemExit(
            f"Venv python not found at {venv_python}. Set up the venv first."
        )

    dest_dir = dest_dir or (Path.home() / "Applications")
    dest_dir.mkdir(parents=True, exist_ok=True)
    app = dest_dir / f"{APP_NAME}.app"
    macos_dir = app / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)

    (app / "Contents" / "Info.plist").write_text(info_plist())
    launcher = macos_dir / APP_NAME
    launcher.write_text(launcher_script(project_root))
    # Make the launcher executable (rwxr-xr-x).
    launcher.chmod(
        launcher.stat().st_mode
        | stat.S_IXUSR
        | stat.S_IXGRP
        | stat.S_IXOTH
    )
    return app
