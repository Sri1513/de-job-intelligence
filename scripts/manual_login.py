#!/usr/bin/env python3
"""One-time manual login for a platform's persistent browser profile.

Usage:
    python scripts/manual_login.py linkedin
    python scripts/manual_login.py indeed

Opens a HEADED browser on data/browser_profiles/<platform>/, navigates to the
platform's login page, and waits for you to log in by hand (including any
2FA). The session is saved directly into the persistent profile, which the
apply worker then reuses on every run — no cookie files to copy, no
re-injection, no expiry from stale seeds.

On a headless Linux server / Docker host, run under a virtual display:
    xvfb-run -a python scripts/manual_login.py linkedin
(requires the system `xvfb` package: `apt install xvfb`)

This script NEVER handles credentials: you type them into the real browser
window yourself. Re-run it any time a session expires.
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.engine.browser_agent import (  # noqa: E402
    PLATFORM_HOME_URL,
    page_shows_logged_in,
    playwright_launch_args,
    profile_dir_for,
)

LOGIN_URLS = {
    "linkedin": "https://www.linkedin.com/login",
    "indeed": "https://secure.indeed.com/auth",
}


async def manual_login(platform: str) -> int:
    from playwright.async_api import async_playwright

    profile_dir = profile_dir_for(platform)
    print(f"\n[1/3] Persistent profile: {profile_dir}")
    print("[2/3] Opening headed browser - log in manually in the window.")
    print("      Complete any 2FA / verification prompts.")
    print("      Do NOT close the window; return here when you are logged in.\n")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=playwright_launch_args(),
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded")

        await asyncio.to_thread(input, "Press ENTER after you have logged in ... ")

        print("\n[3/3] Verifying login state...")
        try:
            await page.goto(
                PLATFORM_HOME_URL[platform], wait_until="domcontentloaded", timeout=25000
            )
            await page.wait_for_timeout(3000)
            state = await page_shows_logged_in(page, platform)
        finally:
            await context.close()

    if state is True:
        print(f"SUCCESS: logged in to {platform}. The apply worker will reuse this session.")
        return 0
    if state is False:
        print(f"FAILED: still logged out of {platform}. Please try again.", file=sys.stderr)
        return 1
    print(
        f"UNCERTAIN: could not confirm login state for {platform} "
        "(ambiguous page). The worker will check again on its next run.",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-time manual login into the persistent browser profile."
    )
    parser.add_argument(
        "platform",
        choices=sorted(LOGIN_URLS),
        help="Platform to log in to (linkedin or indeed).",
    )
    args = parser.parse_args()
    return asyncio.run(manual_login(args.platform))


if __name__ == "__main__":
    raise SystemExit(main())
