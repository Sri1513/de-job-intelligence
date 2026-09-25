# src/ingestion/dice_client.py
import json
import logging
from typing import Any
from fastmcp import Client

logger = logging.getLogger("de-job-intelligence.dice_client")

def normalize_dice_job(raw_job: dict) -> dict:
    """Maps Dice MCP server fields into your standard pipeline schema."""
    location_info = raw_job.get("jobLocation", {})
    location_str = location_info.get("displayName", "Remote") if isinstance(location_info, dict) else "Remote"
    
    sponsorship_flag = raw_job.get("willingToSponsor")
    if sponsorship_flag is True:
        sponsorship = "Available"
    elif sponsorship_flag is False:
        sponsorship = "Not Available"
    else:
        sponsorship = "Not Mentioned"

    raw_date = (
        raw_job.get("postedDate") or 
        raw_job.get("datePosted") or 
        raw_job.get("date_posted") or 
        "Recently"
    )
    posted_date = raw_date.split("T")[0] if isinstance(raw_date, str) and "T" in raw_date else raw_date

    job_description = (
        raw_job.get("jobDescription") or
        raw_job.get("description") or
        raw_job.get("summary") or
        raw_job.get("fullDescription") or
        raw_job.get("text") or
        ""
    )
    
    job_id = str(raw_job.get("id") or raw_job.get("jobId") or "unknown")

    return {
        "job_id": f"dice_{job_id}",
        "title": raw_job.get("title", "Untitled Position"),
        "company": raw_job.get("companyName", "Confidential"),
        "location": location_str,
        "job_url": raw_job.get("detailsPageUrl") or f"https://www.dice.com/job-detail/{job_id}",
        "description": job_description,
        "employment_type": raw_job.get("employmentType", "Unknown"),
        "sponsorship": sponsorship,
        "min_amount": None,
        "max_amount": None,
        "is_remote": raw_job.get("isRemote", False),
        "source": "dice",
        "date_posted": posted_date
    }

class DiceJobClient:
    """MCP client for searching and ingesting job postings from Dice."""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def search_jobs(
        self,
        query: str = "Data Engineer",
        location: str = "United States",
        page: int = 1,
        page_size: int = 20
    ) -> list[dict[str, Any]]:
        """Queries the official Dice MCP server and normalizes the job payloads."""
        import asyncio
        
        async def _fetch():
            dice_jobs = []
            client = Client("https://mcp.dice.com/mcp")
            try:
                async with client:
                    result = await client.call_tool(
                        "search_jobs", 
                        {
                            "keyword": query, 
                            "location": location,
                            "jobs_per_page": page_size
                        }
                    )
                    
                    if hasattr(result, "structured_content") and result.structured_content:
                        data = result.structured_content.get("data", [])
                        dice_jobs = [normalize_dice_job(j) for j in data[:page_size]]
                    elif hasattr(result, "content"):
                        for content in result.content:
                            if getattr(content, "type", None) == "text":
                                parsed = json.loads(content.text)
                                data = parsed.get("data", [])
                                dice_jobs = [normalize_dice_job(j) for j in data[:page_size]]
                                
                logger.info(f"Successfully fetched {len(dice_jobs)} jobs from Dice MCP.")
                return dice_jobs
            except Exception as exc:
                logger.error(f"Failed to fetch jobs from Dice MCP: {exc}")
                return []

        try:
            return asyncio.run(_fetch())
        except RuntimeError:
            # Handle cases where an event loop is already running in the current thread
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(_fetch())