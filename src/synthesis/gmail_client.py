# src/synthesis/gmail_client.py
import base64
import logging
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

from googleapiclient.discovery import build

from src.synthesis.gdrive_docs import get_google_credentials

logger = logging.getLogger("de-job-intelligence.synthesis")


def create_gmail_draft(
    helper_email: str,
    helper_name: str,
    company_name: str,
    job_title: str,
    resume_url: str,
    recipient_email: Optional[str] = None,
    dynamic_email_content: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Creates a rich HTML Gmail draft with precise bullet indentation and formatting:
    - Technical alignment and candidate summary sections rendered as clean indented bullet lists.
    - Automatic bolding of subheadings and keys before colons.
    - Tight vertical spacing in the signature block.
    """
    scopes = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    creds = get_google_credentials(scopes)
    service = build("gmail", "v1", credentials=creds)

    target_recipient = recipient_email or helper_email

    subject = (
        dynamic_email_content.get("subject")
        if dynamic_email_content
        else f"Application for {job_title} – {company_name} – Sri Omkar D"
    )
    body_text = dynamic_email_content.get("body") if dynamic_email_content else ""

    # 1. Convert markdown bold (**text**) to HTML strong tags
    html_body = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", body_text)

    # 2. HTML line parser with robust list indentation control
    lines = html_body.split("\n")
    formatted_lines = []
    state = "normal"  # "normal", "alignment", "summary", "signature"

    for line in lines:
        stripped = line.strip()
        lower_stripped = stripped.lower()

        # Detect section transitions
        if "aligns with your core requirements" in lower_stripped:
            if state in ("alignment", "summary"):
                formatted_lines.append("</ul>")
            state = "alignment"
            formatted_lines.append(
                f'<div style="margin-top: 12px; margin-bottom: 4px;">{line}</div>'
            )
            # Precise cross-client indentation using margin-left and padding-left
            formatted_lines.append(
                '<ul style="margin: 4px 0 8px 0; margin-left: 15px; padding-left: 10px;">'
            )
            continue

        if "candidate summary" in lower_stripped:
            if state in ("alignment", "summary"):
                formatted_lines.append("</ul>")
            state = "summary"
            formatted_lines.append(
                f'<div style="margin-top: 14px; margin-bottom: 4px; font-weight: bold;">{line}</div>'
            )
            formatted_lines.append(
                '<ul style="margin: 4px 0 8px 0; margin-left: 20px; padding-left: 15px;">'
            )
            continue

        if "best regards" in lower_stripped:
            if state in ("alignment", "summary"):
                formatted_lines.append("</ul>")
            state = "signature"
            formatted_lines.append(
                f'<div style="margin-top: 12px; margin-bottom: 2px;">{line}</div>'
            )
            continue

        # Handle bullet items for both alignment and summary sections
        if state in ("alignment", "summary"):
            if stripped.startswith(("o ", "-", "•", "*")):
                bullet_content = re.sub(r"^[o\-\•\*]\s*", "", stripped)
                if ":" in bullet_content and "<strong>" not in bullet_content:
                    parts = bullet_content.split(":", 1)
                    key = parts[0].strip()
                    val = parts[1].strip()
                    bullet_content = f"<strong>{key}:</strong> {val}"
                formatted_lines.append(
                    f'<li style="margin-bottom: 5px; line-height: 1.4;">{bullet_content}</li>'
                )
            elif not stripped:
                continue
            else:
                formatted_lines.append("</ul>")
                state = "normal"
                formatted_lines.append(f'<div style="margin-bottom: 6px;">{line}</div>')

        elif state == "signature":
            if stripped:
                formatted_lines.append(
                    f'<div style="margin-bottom: 1px; line-height: 1.3;">{line}</div>'
                )
            else:
                continue

        else:
            if stripped:
                formatted_lines.append(f'<div style="margin-bottom: 6px;">{line}</div>')
            else:
                formatted_lines.append('<div style="height: 4px;"></div>')

    if state in ("alignment", "summary"):
        formatted_lines.append("</ul>")

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; font-size: 14px; color: #222222; line-height: 1.5;">
        {"".join(formatted_lines)}
      </body>
    </html>
    """

    message = MIMEMultipart("alternative")
    message["To"] = target_recipient
    message["Cc"] = helper_email
    message["Subject"] = subject

    part = MIMEText(html_content, "html")
    message.attach(part)

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    draft_body = {"message": {"raw": raw_message}}

    try:
        draft = service.users().drafts().create(userId="me", body=draft_body).execute()
        draft_id = draft.get("id")
        logger.info(
            f"Successfully created polished HTML Gmail draft ID {draft_id} (CC: {helper_email})"
        )
        return {
            "draft_id": draft_id,
            "subject": subject,
            "recipient": target_recipient,
            "cc": helper_email,
        }
    except Exception as e:
        logger.error(f"Failed to create Gmail draft: {e}", exc_info=True)
        raise
