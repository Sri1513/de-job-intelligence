# src/ingestion/dice_client.py
import logging
from typing import Any

import requests

logger = logging.getLogger("de-job-intelligence.dice_client")

class DiceJobClient:
    """REST client for searching and ingesting job postings from Dice."""

    BASE_URL = "https://spiderbi.dice.com/job-search/api/minimal/bundle"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json"
        })

    def search_jobs(
        self,
        query: str = "Data Engineer",
        location: str = "United States",
        page: int = 1,
        page_size: int = 20
    ) -> list[dict[str, Any]]:
        """Queries the Dice API and normalizes the job payloads."""
        params = {
            "q": query,
            "location": location,
            "page": page,
            "pageSize": page_size,
            "language": "en"
        }
        try:
            response = self.session.get(self.BASE_URL, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            raw_jobs = data.get("data", [])

            normalized_jobs = []
            for item in raw_jobs:
                job_id = str(item.get("id") or item.get("jobId") or "")
                if not job_id:
                    continue

                normalized_jobs.append({
                    "job_id": f"dice_{job_id}",
                    "title": item.get("title", "Untitled Position"),
                    "company": item.get("companyName", "Confidential"),
                    "location": item.get("location", location),
                    "is_remote": "remote" in (item.get("location", "").lower() + item.get("title", "").lower()),
                    "job_url": item.get("detailsPageUrl") or f"https://www.dice.com/job-detail/{job_id}",
                    "description": item.get("summary", ""),
                    "source": "dice"
                })

            return normalized_jobs

        except requests.RequestException as exc:
            logger.error(f"Failed to fetch jobs from Dice API: {exc}")
            return []
