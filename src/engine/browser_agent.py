# In src/engine/browser_agent.py
import base64
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from browser_use import Agent, Browser, ChatGoogle

from src.core.config import settings
from src.core.profile import ApplicantProfile, applicant_profile

logger = logging.getLogger("de-job-intelligence.browser_agent")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCREENSHOT_DIR = PROJECT_ROOT / "data" / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

AUTH_STATES_DIR = PROJECT_ROOT / "config" / "auth_states"
AUTH_STATES_DIR.mkdir(parents=True, exist_ok=True)


# In src/engine/browser_agent.py


def load_combined_storage_state() -> dict | None:
    """Merges and sanitizes all session files in config/auth_states/.
    Strips partitionKey and non-standard attributes that cause CDP deserialization errors.
    """
    if not AUTH_STATES_DIR.exists():
        return None

    raw_cookies = []
    combined_origins = []
    loaded_any = False

    for state_file in AUTH_STATES_DIR.glob("*.json"):
        if state_file.name.startswith("_"):
            continue

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

                if isinstance(data, dict):
                    raw_cookies.extend(data.get("cookies", []))
                    combined_origins.extend(data.get("origins", []))
                    loaded_any = True
                    logger.info("Loaded Playwright auth state from %s", state_file.name)

                elif isinstance(data, list):
                    raw_cookies.extend(data)
                    loaded_any = True
                    logger.info("Loaded Cookie-Editor list from %s", state_file.name)

        except Exception as e:
            logger.warning("Could not parse auth state %s: %s", state_file.name, e)

    if not loaded_any:
        return None

    # Sanitize each cookie to only standard CDP fields
    unique_cookies = []
    seen = set()

    for c in raw_cookies:
        name = c.get("name")
        domain = c.get("domain")
        path = c.get("path", "/")

        if not name or not c.get("value"):
            continue

        key = (name, domain, path)
        if key in seen:
            continue
        seen.add(key)

        cookie_dict = {
            "name": str(name),
            "value": str(c.get("value")),
            "domain": str(domain),
            "path": str(path),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }

        # Validate expires timestamp
        exp = c.get("expires") or c.get("expirationDate")
        if exp is not None:
            try:
                exp_float = float(exp)
                if exp_float > 0:
                    cookie_dict["expires"] = exp_float
            except (ValueError, TypeError):
                pass

        # Normalize sameSite
        same_site = c.get("sameSite")
        if same_site in ["Strict", "Lax", "None"]:
            cookie_dict["sameSite"] = same_site

        # CRITICAL: partitionKey is deliberately omitted to prevent CDP CBOR parse failure
        unique_cookies.append(cookie_dict)

    merged_state = {
        "cookies": unique_cookies,
        "origins": combined_origins,
    }

    merged_file = AUTH_STATES_DIR / "_active_session.json"
    with open(merged_file, "w", encoding="utf-8") as f:
        json.dump(merged_state, f, indent=2)

    return merged_state


async def autofill_job_application(
    job_url: str,
    job_id: str,
    profile: ApplicantProfile = applicant_profile,
    resume_path: Path | str | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    api_key = settings.GEMINI_API_KEY
    if api_key:
        os.environ["GOOGLE_API_KEY"] = api_key
        os.environ["GEMINI_API_KEY"] = api_key

    llm = ChatGoogle(
        model="gemini-3.8-flash",
        api_key=api_key,
    )

    # Inside autofill_job_application()
    storage_state_data = load_combined_storage_state()
    active_session_file = AUTH_STATES_DIR / "_active_session.json"

    # Pass the active session file if available
    if storage_state_data and active_session_file.exists():
        browser = Browser(
            headless=headless,
            storage_state=str(active_session_file),
        )
    else:
        browser = Browser(headless=headless)

    # Resolve target resume path
    target_resume: Path | None = None
    if resume_path:
        p = Path(resume_path)
        target_resume = p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
    else:
        target_resume = profile.get_resolved_resume_path(PROJECT_ROOT)

    available_files: list[str] = []
    resume_instruction = ""
    if target_resume and target_resume.exists():
        resolved_str = str(target_resume.resolve())
        available_files.append(resolved_str)
        resume_instruction = f"""
RESUME UPLOAD:
- Candidate Resume Path: {resolved_str}
- Locate the Resume / CV upload element or button (e.g. 'Attach Resume', 'Upload CV', 'input[type=file]').
- Use the 'upload_file' action to attach the resume file from the path above.
"""
    else:
        logger.warning("No valid resume file found on disk. Proceeding without file attachment.")

    screenshot_file = SCREENSHOT_DIR / f"{job_id}_review.png"

    task_instructions = f"""
You are an autonomous job application agent.
Navigate to this job application URL: {job_url}

CANDIDATE PROFILE:
First Name: {profile.first_name}
Last Name: {profile.last_name}
Full Name: {profile.full_name}
Email: {profile.email}
Phone: {profile.phone}
Location: {profile.location}
LinkedIn: {profile.linkedin}
Portfolio/Website: {profile.website or "N/A"}
GitHub: {profile.github or "N/A"}
Authorized to work in US: {"Yes" if profile.work_authorization.authorized_in_us else "No"}
Requires Visa Sponsorship: {"Yes" if profile.work_authorization.requires_sponsorship_now_or_future else "No"}
{resume_instruction}

TASK RULES:
1. Locate the application form (click 'Apply' or 'Apply Now' if not already on the form).
2. Populate the standard contact and profile inputs.
3. For Work Authorization / Sponsorship questions, match the candidate profile values above.
4. For custom dropdowns or comboboxes (e.g. React-Select), click the dropdown option or press Enter to ensure the value is selected and no longer shows 'Select...'.
5. If a resume upload field is present, upload the resume using the file path provided.
6. If an external ATS link (Greenhouse, Lever, Workday) is blocked by a login modal, you are allowed to locate the direct ATS posting.
7. CRITICAL HUMAN GATE: DO NOT click 'Submit', 'Submit Application', or any final confirmation button.
8. Once all inputs are filled and the resume is attached, call 'done' with the summary of filled fields.
"""

    agent = Agent(
        task=task_instructions,
        llm=llm,
        browser=browser,
        available_file_paths=available_files,
    )

    # In src/engine/browser_agent.py

    logger.info("Executing browser agent for job_id=%s...", job_id)
    try:
        history = await agent.run(max_steps=30)

        # 1. Determine actual agent success
        is_success = history.is_successful() if hasattr(history, "is_successful") else True
        final_summary = history.final_result() if hasattr(history, "final_result") else None

        # 2. Extract verification screenshot
        saved_screenshot = False
        try:
            paths = history.screenshot_paths(return_none_if_not_screenshot=False)
            if paths:
                last_path = Path(paths[-1])
                if last_path.exists():
                    shutil.copy2(last_path, screenshot_file)
                    logger.info(
                        "Copied review screenshot from %s to %s", last_path, screenshot_file
                    )
                    saved_screenshot = True
        except Exception as err:
            logger.debug("Failed reading screenshot_paths: %s", err)

        if not saved_screenshot:
            try:
                b64_list = history.screenshots(return_none_if_not_screenshot=False)
                if b64_list:
                    raw_b64 = b64_list[-1]
                    if "," in raw_b64:
                        raw_b64 = raw_b64.split(",", 1)[1]
                    screenshot_file.write_bytes(base64.b64decode(raw_b64))
                    logger.info("Decoded review screenshot from history to %s", screenshot_file)
                    saved_screenshot = True
            except Exception as err:
                logger.warning("Failed decoding screenshot from history: %s", err)

        verified_screenshot_path = (
            str(screenshot_file) if (saved_screenshot and screenshot_file.exists()) else None
        )
        # 3. Route status based on task execution result
        if not is_success:
            logger.warning("Agent reported task failure for job_id=%s: %s", job_id, final_summary)
            return {
                "status": "FAILED",
                "job_id": job_id,
                "screenshot_path": verified_screenshot_path,
                "message": final_summary
                or "Agent was blocked by auth walls or CAPTCHAs before completing form.",
            }

        return {
            "status": "PENDING_REVIEW",
            "job_id": job_id,
            "screenshot_path": verified_screenshot_path,
            "message": final_summary
            or "Application autofilled with resume attached and ready for review.",
        }
    except Exception as exc:
        logger.error("Browser agent failed on job_id=%s: %s", job_id, exc)
        return {
            "status": "FAILED",
            "job_id": job_id,
            "error": str(exc),
        }
    finally:
        await browser.close()
