"""Tests for the macOS .app bundle builder (no actual launch)."""

from __future__ import annotations

from pathlib import Path

from src.macos_app import info_plist, launcher_script


def test_info_plist_has_required_keys() -> None:
    body = info_plist()
    for key in (
        "CFBundleName",
        "CFBundleIdentifier",
        "CFBundleExecutable",
        "CFBundlePackageType",
    ):
        assert f"<key>{key}</key>" in body
    assert "<string>APPL</string>" in body
    assert "TradBot" in body


def test_launcher_script_bakes_project_root() -> None:
    root = Path("/Users/someone/TRAD_BOT")
    script = launcher_script(root)
    assert script.startswith("#!/bin/bash")
    # Project root and venv python are baked in.
    assert "/Users/someone/TRAD_BOT" in script
    assert "/.venv/bin/python" in script
    # It runs the interactive menu via osascript → Terminal.
    assert "scripts.tradbot menu" in script
    assert "osascript" in script
    assert 'tell application "Terminal"' in script


def test_launcher_script_runnable_shape() -> None:
    """The launcher must be a single self-contained bash script."""
    script = launcher_script(Path("/tmp/x"))
    # No unresolved format placeholders.
    assert "{" not in script.replace("{}", "")  # heredoc has no braces here
    # Heredoc is balanced.
    assert script.count("APPLESCRIPT") == 2
