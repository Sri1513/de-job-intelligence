# src/synthesis/gdrive_docs.py
from pathlib import Path
from typing import Dict, Any, List
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from src.core.config import settings

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive"
]

# Template Document ID (Original verified Google Doc template)
DEFAULT_TEMPLATE_ID = "1ilVQ2WozlBasSra70fBqFmRMxo8Lyb9DbgtTKwQXlAQ"

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

def get_google_credentials() -> Credentials:
    """
    Acquires or refreshes OAuth 2.0 credentials.
    Supports persistent refresh tokens generated for production.
    """
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"OAuth credentials.json not found at {CREDENTIALS_FILE}. "
                    "Please provide credentials.json to authorize Google Workspace API."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return creds

def create_tailored_document(
    replacements: Dict[str, str],
    document_title: str,
    template_id: str = None
) -> str:
    """
    Copies the base resume template, executes batch text replacements,
    and returns the public edit URL of the generated Google Doc.
    """
    target_template_id = template_id or settings.RESUME_TEMPLATE_DOC_ID
    if not target_template_id:
        raise ValueError("GOOGLE_DOCS_TEMPLATE_ID is not configured in settings or .env.")

    creds = get_google_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    docs_service = build("docs", "v1", credentials=creds)

    # 1. Copy the base template document
    copy_body = {"name": document_title}
    copied_file = drive_service.files().copy(
        fileId=target_template_id,
        body=copy_body
    ).execute()
    new_doc_id = copied_file.get("id")

    # 2. Build batch replacement requests
    requests: List[Dict[str, Any]] = []
    for placeholder, text in replacements.items():
        requests.append({
            "replaceAllText": {
                "containsText": {
                    "text": placeholder,
                    "matchCase": True
                },
                "replaceText": text
            }
        })

    # 3. Execute batch updates on Google Docs API
    docs_service.documents().batchUpdate(
        documentId=new_doc_id,
        body={"requests": requests}
    ).execute()

    return f"https://docs.google.com/document/d/{new_doc_id}/edit"