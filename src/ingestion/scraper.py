# src/ingestion/scraper.py
import re
import logging
from typing import Optional
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("de-job-intelligence.scraper")

def fetch_and_clean_job_page(url: str, timeout: int = 10) -> str:
    """
    Fetches raw HTML from a job posting URL, removes script/style tags,
    and returns sanitized text.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        raw_text = soup.get_text(separator=" ")
        cleaned = re.sub(r"\s+", " ", raw_text).strip()
        return cleaned
    except Exception as exc:
        logger.error(f"Failed to scrape webpage at {url}: {exc}")
        return ""