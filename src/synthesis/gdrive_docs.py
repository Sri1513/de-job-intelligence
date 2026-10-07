# src/synthesis/gdrive_docs.py
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src.core.config import settings
from src.synthesis.resume_mapper import build_replacement_payload

logger = logging.getLogger("de-job-intelligence.synthesis")

# Comprehensive scopes covering Docs, Drive, and Gmail draft creation
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.compose",
]


def get_google_credentials(scopes: List[str] = None) -> Credentials:
    """Acquires and refreshes Google OAuth 2.0 user credentials with dynamic multi-service scopes."""
    if scopes is None:
        scopes = DEFAULT_SCOPES

    creds = None
    token_path = (
        Path(settings.BASE_DIR) / "token.json"
        if hasattr(settings, "BASE_DIR")
        else Path("token.json")
    )
    creds_path = (
        Path(settings.BASE_DIR) / "credentials.json"
        if hasattr(settings, "BASE_DIR")
        else Path("credentials.json")
    )

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except Exception as e:
            logger.warning(
                f"Existing token.json scope mismatch or invalid: {e}. Re-authenticating..."
            )
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request

            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"Failed to refresh token: {e}. Starting fresh auth flow...")
                creds = None

        if not creds:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Missing {creds_path}. Place credentials.json in the project root."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as token:
            token.write(creds.to_json())

    return creds


def get_or_create_folder(drive_service, folder_name: str, parent_id: str = None) -> str:
    """Searches for a folder by name within a parent ID. Creates it if it doesn't exist."""
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    response = (
        drive_service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
    )
    files = response.get("files", [])

    if files:
        return files[0]["id"]

    folder_metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        folder_metadata["parents"] = [parent_id]

    folder = drive_service.files().create(body=folder_metadata, fields="id").execute()
    return folder.get("id")


def parse_and_strip_markdown(raw_text: str) -> Tuple[str, List[str]]:
    """Extracts explicit bold phrases marked with **term** and returns clean text and terms."""
    if not isinstance(raw_text, str):
        return str(raw_text or ""), []

    bold_terms = []
    for match in re.finditer(r"\*\*([^*]+)\*\*", raw_text):
        clean_match = match.group(1).strip()
        if len(clean_match) >= 2:
            bold_terms.append(clean_match)

    cleaned_text = re.sub(r"\*\*([^*]+)\*\*", r"\1", raw_text)
    return cleaned_text, bold_terms


def apply_semantic_bolding(
    docs_service,
    document_id: str,
    summary_terms: List[str],
    bullet_terms: Dict[str, List[str]],
) -> None:
    """Bolds high-signal technical terms in Summary and Experience sections."""
    doc = docs_service.documents().get(documentId=document_id).execute()
    content = doc.get("body", {}).get("content", [])

    SKIP_SECTIONS = {"technical skills", "skills", "education", "contact"}
    TARGET_SECTIONS = {"professional summary", "summary", "professional experience", "experience"}

    current_section = "header"
    requests: List[Dict[str, Any]] = []
    log_summary: List[str] = []

    unique_bullet_terms = set()
    for terms in bullet_terms.values():
        for t in terms:
            unique_bullet_terms.add(t)

    sorted_summary_terms = sorted(list(set(summary_terms)), key=len, reverse=True)
    sorted_bullet_terms = sorted(list(unique_bullet_terms), key=len, reverse=True)

    for elem in content:
        if "paragraph" not in elem:
            continue

        elements = elem["paragraph"].get("elements", [])
        para_text = "".join(pe.get("textRun", {}).get("content", "") for pe in elements).strip()
        para_lower = para_text.lower()

        if len(para_text) < 45:
            for s in SKIP_SECTIONS:
                if s in para_lower:
                    current_section = "skip"
                    break
            for t in TARGET_SECTIONS:
                if t in para_lower:
                    if "summary" in t:
                        current_section = "summary"
                    else:
                        current_section = "experience"
                    break

        if current_section not in {"summary", "experience"} or not para_text:
            continue

        active_terms = sorted_summary_terms if current_section == "summary" else sorted_bullet_terms

        for pe in elements:
            text_run = pe.get("textRun")
            if not text_run or text_run.get("textStyle", {}).get("bold"):
                continue

            content_str = text_run.get("content", "")
            base_idx = pe.get("startIndex", 0)
            occupied_ranges: List[Tuple[int, int]] = []

            for term in active_terms:
                pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
                for match in re.finditer(pattern, content_str, re.IGNORECASE):
                    s, e = match.start(), match.end()
                    if any(os <= s < oe or os < e <= oe for os, oe in occupied_ranges):
                        continue

                    occupied_ranges.append((s, e))
                    requests.append(
                        {
                            "updateTextStyle": {
                                "range": {"startIndex": base_idx + s, "endIndex": base_idx + e},
                                "textStyle": {"bold": True},
                                "fields": "bold",
                            }
                        }
                    )
                    log_summary.append(f"[{current_section.upper()}] '{term}'")

    if requests:
        docs_service.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()
        print(f"[Purposeful Bolder] Successfully styled {len(requests)} entities.")
    else:
        print("[Purposeful Bolder] No terms required styling.")


def create_tailored_document(
    replacements: Dict[str, str],
    document_title: str,
    template_id: str = None,
    summary_terms: List[str] = None,
    bullet_terms: Dict[str, List[str]] = None,
) -> str:
    """Clones template, organizes into Drive folder structure, executes replacements, and bolds text."""
    target_template = (
        template_id
        or getattr(settings, "RESUME_TEMPLATE_DOC_ID", None)
        or getattr(settings, "GOOGLE_DOCS_TEMPLATE_ID", None)
    )
    if not target_template:
        raise ValueError(
            "Neither RESUME_TEMPLATE_DOC_ID nor GOOGLE_DOCS_TEMPLATE_ID is configured."
        )

    creds = get_google_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    docs_service = build("docs", "v1", credentials=creds)

    company_name = "General Applications"
    if " - " in document_title:
        company_name = document_title.split(" - ")[0].strip()

    clean_file_name = "Sri Omkar - Data Engineer"

    master_folder_id = get_or_create_folder(drive_service, "Resumes and Cover Letters")
    company_folder_id = get_or_create_folder(
        drive_service, company_name, parent_id=master_folder_id
    )

    copied_file = (
        drive_service.files()
        .copy(
            fileId=target_template,
            body={"name": clean_file_name, "parents": [company_folder_id]},
        )
        .execute()
    )
    new_doc_id = copied_file.get("id")

    print(f"[Drive] Placed resume in 'Resumes and Cover Letters/{company_name}/{clean_file_name}'")

    requests: List[Dict[str, Any]] = []
    for placeholder, text in replacements.items():
        requests.append(
            {
                "replaceAllText": {
                    "containsText": {"text": placeholder, "matchCase": True},
                    "replaceText": text or "",
                }
            }
        )

    docs_service.documents().batchUpdate(
        documentId=new_doc_id,
        body={"requests": requests},
    ).execute()

    try:
        apply_semantic_bolding(
            docs_service=docs_service,
            document_id=new_doc_id,
            summary_terms=summary_terms or [],
            bullet_terms=bullet_terms or {},
        )
    except Exception as e:
        logger.error(f"Semantic bolding encountered an error: {e}", exc_info=True)

    return f"https://docs.google.com/document/d/{new_doc_id}/edit"


def generate_resume_from_llm_payload(
    llm_payload: Dict[str, Any],
    document_title: str,
    slot_headers: Dict[str, Dict[str, str]] | None = None,
) -> str:
    """Saves raw payload, parses markdown bold selections, and maps cleanly to Google Docs."""
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "last_tailoring_payload.json").write_text(
        json.dumps(llm_payload, indent=2, default=str), encoding="utf-8"
    )

    raw_summary = llm_payload.get("summary") or llm_payload.get("professional_summary") or ""
    clean_summary, summary_terms = parse_and_strip_markdown(raw_summary)

    bullet_terms: Dict[str, List[str]] = {}
    cleaned_exp: Dict[str, List[str]] = {}

    raw_exp = llm_payload.get("experience_bullets") or llm_payload.get("experience") or {}

    if isinstance(raw_exp, dict):
        for role_key, b_list in raw_exp.items():
            cleaned_exp[role_key] = []
            bullet_terms[role_key] = []
            if isinstance(b_list, list):
                for item in b_list:
                    c_txt, b_terms = parse_and_strip_markdown(item)
                    cleaned_exp[role_key].append(c_txt)
                    bullet_terms[role_key].extend(b_terms)

    cleaned_payload = dict(llm_payload)
    if clean_summary:
        cleaned_payload["professional_summary"] = clean_summary
        cleaned_payload["summary"] = clean_summary
    if cleaned_exp:
        cleaned_payload["experience_bullets"] = cleaned_exp

    replacements = build_replacement_payload(cleaned_payload, slot_headers=slot_headers)

    (logs_dir / "last_styling_terms.json").write_text(
        json.dumps({"summary_terms": summary_terms, "bullet_terms": bullet_terms}, indent=2),
        encoding="utf-8",
    )

    return create_tailored_document(
        replacements=replacements,
        document_title=document_title,
        summary_terms=summary_terms,
        bullet_terms=bullet_terms,
    )
