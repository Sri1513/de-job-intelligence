# scripts/test_browser_agent.py
import asyncio

from src.engine.browser_agent import autofill_job_application


async def main():
    # Target any publicly accessible application form (e.g. Lever, Greenhouse, or Ashby)
    test_url = "https://jobs.lever.co/leverdemo/7b1c4e75-b46f-4024-8b64-8840a1b24bf4"
    test_id = "test_run_001"

    print(f"Launching headful browser agent against: {test_url}")
    result = await autofill_job_application(
        job_url=test_url,
        job_id=test_id,
        headless=False,
    )
    print("\nResult:", result)


if __name__ == "__main__":
    asyncio.run(main())
