# src/ingestion/email_scraper.py
import email
import imaplib
import os
import re
from datetime import datetime
from email.header import decode_header

TARGET_KEYWORDS = [
    "data engineer",
    "etl",
    "data pipeline",
    "databricks",
    "snowflake",
    "analytics engineer",
    "big data",
    "python developer",
]

TITLE_MAX_LEN = 150
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def clean_job_title(raw_title: str) -> str | None:
    """
    Sanitize a title parsed from an email body.

    LinkedIn sends several alert formats (job alerts, "jobs viewed" reminders,
    "similar jobs" digests). Reminder-style bodies mash the subject line together
    with tracking URLs, so the naive "first line = title" parse produces garbage
    that breaks downstream display. This strips URLs/tracking params, collapses
    whitespace, truncates, and rejects anything that isn't plausibly a job title.
    Returns None when the chunk should be skipped entirely.
    """
    if not raw_title:
        return None
    title = _URL_RE.sub("", raw_title)
    title = re.sub(r"\s+", " ", title).strip(" \t-–—:;,.\"'")
    if len(title) < 8:
        return None
    if len(title) > TITLE_MAX_LEN:
        title = title[:TITLE_MAX_LEN].rstrip() + "…"
    return title


def parse_linkedin_plain_text_email(body_text: str) -> list:
    """Parses LinkedIn alert blocks separated by dashed lines into structured job records."""
    extracted_jobs = []
    chunks = body_text.split("---------------------------------------------------------")

    for chunk in chunks:
        lines = [line.strip() for line in chunk.split("\n") if line.strip()]
        if len(lines) < 3:
            continue

        title = lines[0]

        if any(
            bad in title.lower()
            for bad in [
                "your job alert",
                "new jobs",
                "see all jobs",
                "unsubscribe",
                "this email was",
            ]
        ):
            continue

        # Reminder-style emails mash subject text + tracking URLs into the
        # "title" line; clean it and skip the chunk if nothing usable remains.
        title = clean_job_title(title)
        if not title:
            continue

        company = lines[1]
        location = "United States"
        job_url = "https://www.linkedin.com"

        for line in lines[2:]:
            if line.startswith("View job:"):
                url_parts = line.split("View job:")
                if len(url_parts) > 1:
                    job_url = url_parts[1].strip()
            elif not any(
                badge in line.lower()
                for badge in ["company alum", "actively hiring", "apply with", "view job"]
            ):
                if location == "United States":
                    location = line

        extracted_jobs.append(
            {
                "title": title,
                "company": company if company else "Unknown",
                "location": location if location else "United States",
                "job_url": job_url,
                "description": f"Extracted via email alert for {title} at {company}",
                "employment_type": "Unknown",
                "sponsorship": "Not Mentioned",
                "source": "Email Alert",
                "date_posted": datetime.now().strftime("%Y-%m-%d"),
            }
        )

    return extracted_jobs


def fetch_jobs_from_email(limit: int = 10) -> list:
    """Connects to Gmail securely, pulls LinkedIn job alerts from multiple senders, and filters locally."""
    email_user = os.getenv("EMAIL_USER")
    email_password = os.getenv("EMAIL_PASSWORD")

    if not email_user or not email_password:
        print("EMAIL_USER or EMAIL_PASSWORD not set in environment variables.")
        return []

    all_jobs = []
    target_senders = ["jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"]

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_user, email_password)
        mail.select("INBOX")

        all_email_ids = set()

        # Collect email IDs from both LinkedIn sender addresses
        for sender in target_senders:
            status, messages = mail.search(None, f'(FROM "{sender}")')
            if status == "OK" and messages[0]:
                for e_id in messages[0].split():
                    all_email_ids.add(e_id)

        if not all_email_ids:
            print("No emails found from LinkedIn job alert addresses.")
            return []

        # Sort and take the most recent ones up to the limit
        sorted_recent_ids = sorted(list(all_email_ids), key=int)[-limit:]

        for e_id in reversed(sorted_recent_ids):
            res, msg_data = mail.fetch(e_id, "(RFC822)")
            if res != "OK":
                continue

            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject_header = decode_header(msg["Subject"])[0]
                    subject = subject_header[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(subject_header[1] or "utf-8", errors="ignore")

                    if not any(kw in subject.lower() for kw in TARGET_KEYWORDS):
                        continue

                    print(f"Processing relevant email alert: '{subject}'")

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body += payload.decode("utf-8", errors="ignore")
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="ignore")

                    parsed_jobs = parse_linkedin_plain_text_email(body)
                    all_jobs.extend(parsed_jobs)

        mail.logout()
        print(f"Successfully extracted {len(all_jobs)} clean jobs from email alerts.")
    except Exception as e:
        print(f"Email connection/scraping error: {e}")

    return all_jobs
