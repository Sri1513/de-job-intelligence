# scripts/export_session.py
import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

START_URLS = {
    "indeed": "https://www.indeed.com",
    "linkedin": "https://www.linkedin.com/login",
    "dice": "https://www.dice.com/dashboard/login",
    "glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = PROJECT_ROOT / "config" / "auth_states"
AUTH_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR = PROJECT_ROOT / "data" / ".browser_profiles"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


async def capture_session(site: str):
    start_url = START_URLS.get(site.lower())
    if not start_url:
        print(f"Unknown site '{site}'. Known options: {list(START_URLS.keys())}")
        return

    output_path = AUTH_DIR / f"{site.lower()}.json"
    user_data_dir = str(TEMP_DIR / f"profile_{site.lower()}")

    print(f"\n[1/3] Launching persistent browser for {site}...")
    async with async_playwright() as p:
        # Using Edge avoids port and process conflicts with your open Chrome
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel="msedge",
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                    "--no-sandbox",
                ],
                ignore_default_args=["--enable-automation"],
                viewport=None,
            )
        except Exception:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                viewport=None,
            )

        page = context.pages[0] if context.pages else await context.new_page()

        print(f"[2/3] Navigating to: {start_url}")
        await page.goto(start_url, wait_until="domcontentloaded")

        if site.lower() == "indeed":
            # If on Indeed homepage, navigate to the sign-in screen with active cookies
            await page.goto("https://secure.indeed.com/auth", wait_until="domcontentloaded")

        print("\n" + "=" * 65)
        print(" ACTION REQUIRED:")
        print(" 1. Complete your login in the opened browser window.")
        print(" 2. Complete any MFA or Google sign-in prompts.")
        print(" 3. Once on your dashboard/feed, return here and press ENTER.")
        print("=" * 65 + "\n")

        input("Press ENTER after completing login...")

        await context.storage_state(path=str(output_path))
        print("\n[3/3] Session successfully saved to:")
        print(f"      {output_path.resolve()}\n")

        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export browser session state.")
    parser.add_argument(
        "--site", type=str, default="indeed", help="Site name (indeed, linkedin, etc.)"
    )
    args = parser.parse_args()

    asyncio.run(capture_session(args.site))
