import glob
import os
import subprocess
import sys


_SETUP_DONE = False


def _playwright_browser_present() -> bool:
    """Return True when a Chromium browser binary is already installed."""
    browser_globs = [
        "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    ]
    for pattern in browser_globs:
        if glob.glob(os.path.expanduser(pattern)):
            return True
    return False


def ensure_playwright_installed() -> bool:
    """Ensure Playwright Chromium is installed without re-running heavy setup.

    This function validates that the Playwright package is importable and that a
    Chromium browser binary is available. It will attempt to install the browser
    if it's not present. Only mark setup as done on success.
    """
    global _SETUP_DONE
    if _SETUP_DONE:
        return True

    try:
        # Validate Playwright import first; this also avoids noisy setup when package is missing.
        import playwright  # noqa: F401

        if not _playwright_browser_present():
            print("Installing Playwright Chromium browser...")
            env = os.environ.copy()
            env["CI"] = "1"
            env["DEBIAN_FRONTEND"] = "noninteractive"
            subprocess.check_call(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                env=env,
            )
    except Exception as e:
        print(f"Warning: Could not install Playwright: {e}")
        return False

    # Only set the flag when setup completed without exception
    _SETUP_DONE = True
    return True
