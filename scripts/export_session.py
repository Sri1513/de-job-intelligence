# scripts/export_session.py
import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import Error, async_playwright

START_URLS = {
    "linkedin": "https://www.linkedin.com/login",
    "indeed": "https://secure.indeed.com/auth",
    "dice": "https://www.dice.com/dashboard/login",
    "glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_DIR = PROJECT_ROOT / "config" / "auth_states"
AUTH_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR = PROJECT_ROOT / "data" / ".browser_profiles"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


async def capture_session(site: str):
    target_site = site.lower().strip()
    start_url = START_URLS.get(target_site)
    if not start_url:
        print(f"Unknown site '{site}'. Known options: {list(START_URLS.keys())}")
        return

    output_path = AUTH_DIR / f"{target_site}.json"
    user_data_dir = str(TEMP_DIR / f"profile_{target_site}")

    print(f"\n[1/3] Launching persistent browser for {target_site}...")
    async with async_playwright() as p:
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

        print("\n" + "=" * 65)
        print(" ACTION REQUIRED:")
        print(" 1. Complete your login in the opened browser window.")
        print(" 2. Complete any 2FA, PIN, or verification prompts.")
        print(" 3. Wait until you land on your feed/dashboard.")
        print(" 4. DO NOT close the browser window.")
        print(" 5. Return to this terminal and press ENTER.")
        print("=" * 65 + "\n")

        # Run input in a worker thread so the asyncio event loop keeps running
        await asyncio.to_thread(input, "Press ENTER after completing login...")

        try:
            state = await context.storage_state()
        except Error as e:
            if "closed" in str(e).lower():
                print("\n❌ Error: The browser window was closed before the session could be captured.")
                print("Please leave the window open until you press ENTER.")
                return
            raise

        # Sanitize cookies: strip partitionKey which causes CDP schema crashes in headless Linux Docker
        cleaned_cookies = []
        for cookie in state.get("cookies", []):
            cookie.pop("partitionKey", None)
            cleaned_cookies.append(cookie)
        state["cookies"] = cleaned_cookies

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        print("\n[3/3] Session successfully captured, sanitized, and saved to:")
        print(f"      {output_path.resolve()}\n")

        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export browser session state.")
    parser.add_argument(
        "site",
        type=str,
        nargs="?",
        default="linkedin",
        help="Site name (linkedin, indeed, dice, glassdoor). Defaults to linkedin.",
    )
    args = parser.parse_args()

    asyncio.run(capture_session(args.site))