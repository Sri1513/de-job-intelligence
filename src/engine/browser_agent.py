# src/engine/browser_agent.py
"""Browser automation agent for job applications.

Session model (persistent-profile-first):
  - ``data/browser_profiles/<platform>/`` is the source of truth. It is a
    Playwright persistent browser profile: cookies, localStorage and
    Cloudflare clearance survive restarts, and the site refreshes the session
    on every run so ``li_at``-style cookies effectively never expire.
  - ``config/auth_states/*.json`` files are a FIRST-BOOT SEED ONLY. They are
    injected exactly once, and only when the profile shows logged-out. They
    are never re-injected over a healthy session: re-injecting stale seeds
    overwrites fresh cookies and *causes* expiry.

To (re)create a session on a new machine, run:
    python scripts/manual_login.py linkedin   # or: indeed
and log in once in the headed browser window. No credentials are ever
handled by this module or that script.
"""

import asyncio
import base64
import inspect
import json
import logging
import os
import random
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from browser_use import Agent, Browser

from src.core.config import settings
from src.core.profile import ApplicantProfile, applicant_profile
from src.engine.llm_router import (
    build_gemini_llm,
    build_openai_compat_llm,
    get_apply_llm,
)

logger = logging.getLogger("de-job-intelligence.browser_agent")

# Status vocabulary produced by autofill_job_application().
STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_SUBMITTED = "SUBMITTED"  # reserved: only set via explicit human approval
STATUS_FAILED = "FAILED"
STATUS_NEEDS_HUMAN = "NEEDS_HUMAN"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCREENSHOT_DIR = Path(settings.SCREENSHOTS_DIR)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

AUTH_STATES_DIR = Path(settings.AUTH_STATES_DIR)
AUTH_STATES_DIR.mkdir(parents=True, exist_ok=True)

PROFILES_ROOT = PROJECT_ROOT / "data" / "browser_profiles"
PROFILES_ROOT.mkdir(parents=True, exist_ok=True)

PLATFORMS = ("linkedin", "indeed")

PLATFORM_HOME_URL = {
    "linkedin": "https://www.linkedin.com/feed/",
    "indeed": "https://www.indeed.com/",
}

# Cookie domain hints used to filter seed cookies per platform.
PLATFORM_COOKIE_DOMAINS = {
    "linkedin": ("linkedin.",),
    "indeed": ("indeed.",),
}

LOGIN_CHECK_TIMEOUT_MS = 25_000


def detect_platform(job_url: str) -> str:
    """Maps a job URL to a session platform: 'linkedin', 'indeed' or 'generic'."""
    host = urlparse(job_url or "").netloc.lower()
    if "linkedin" in host:
        return "linkedin"
    if "indeed" in host:
        return "indeed"
    return "generic"


def profile_dir_for(platform: str) -> Path:
    """Persistent Playwright profile directory for a platform (source of truth)."""
    path = PROFILES_ROOT / platform
    path.mkdir(parents=True, exist_ok=True)
    return path


def playwright_launch_args() -> list[str]:
    # --no-sandbox / --disable-gpu: required on GPU-less Linux servers & Docker.
    # --disable-blink-features=AutomationControlled: reduces headless detection.
    return [
        "--no-sandbox",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
    ]


# ---------------------------------------------------------------------------
# Seed cookie handling (first-boot only)
# ---------------------------------------------------------------------------


def _sanitize_cookies(raw_cookies: list[dict]) -> list[dict]:
    """Keeps only standard cookie fields; drops nameless and expired cookies."""
    now = time.time()
    unique: list[dict] = []
    seen: set[tuple] = set()
    for c in raw_cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        domain = c.get("domain")
        path = c.get("path", "/")
        if not name or not c.get("value"):
            continue
        key = (name, domain, path)
        if key in seen:
            continue
        seen.add(key)

        exp = c.get("expires") or c.get("expirationDate")
        if exp is not None:
            try:
                if float(exp) <= now - 60:
                    logger.debug("Dropping expired seed cookie %s (%s)", name, domain)
                    continue
            except (TypeError, ValueError):
                pass

        cookie_dict = {
            "name": str(name),
            "value": str(c.get("value")),
            "domain": str(domain),
            "path": str(path),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }
        if exp is not None:
            try:
                exp_float = float(exp)
                if exp_float > 0:
                    cookie_dict["expires"] = exp_float
            except (TypeError, ValueError):
                pass

        same_site = c.get("sameSite")
        if same_site in ("Strict", "Lax", "None"):
            cookie_dict["sameSite"] = same_site

        # NOTE: partitionKey is deliberately omitted (CDP CBOR parse failure).
        unique.append(cookie_dict)
    return unique


def load_seed_cookies(platform: str) -> list[dict]:
    """Loads first-boot seed cookies for a platform.

    Prefers ``config/auth_states/<platform>*.json``; falls back to any
    non-underscore-prefixed seed file for backward compatibility. Returns []
    when no usable seed exists. Callers must treat seeds as one-shot: inject
    only when the persistent profile shows logged-out, never over a healthy
    session.
    """
    candidates = sorted(AUTH_STATES_DIR.glob(f"{platform}*.json"))
    if not candidates:
        candidates = sorted(
            p for p in AUTH_STATES_DIR.glob("*.json") if not p.name.startswith("_")
        )
    hints = PLATFORM_COOKIE_DOMAINS.get(platform, ())
    raw: list[dict] = []
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("Could not parse seed file %s: %s", path.name, exc)
            continue
        if isinstance(data, dict):
            raw.extend(data.get("cookies", []))
        elif isinstance(data, list):
            raw.extend(data)
        logger.info("Loaded seed cookies from %s", path.name)

    cookies = _sanitize_cookies(raw)
    if hints:
        cookies = [c for c in cookies if any(h in (c.get("domain") or "") for h in hints)]
    if raw and not cookies:
        logger.warning("Seed cookies for %s were all expired or filtered out.", platform)
    return cookies


# ---------------------------------------------------------------------------
# Login-state detection (persistent profile)
# ---------------------------------------------------------------------------


async def page_shows_logged_in(page: Any, platform: str) -> bool | None:
    """Heuristic login detection on an already-loaded home page.

    Returns True (logged in), False (logged out) or None (ambiguous — the
    caller should proceed and let the agent surface any wall).
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        return None
    if not url or url == "about:blank":
        return None

    if platform == "linkedin":
        if "/login" in url or "authwall" in url:
            return False
        try:
            if await page.query_selector('input[name="session_key"]'):
                return False
        except Exception:
            return None
        return True

    if platform == "indeed":
        if "account/login" in url:
            return False
        try:
            sign_in = await page.query_selector('a[href*="account/login"]')
            if sign_in and await sign_in.is_visible():
                return False
        except Exception:
            return None
        # Indeed's home page is ambiguous when logged in; don't block on it.
        return None

    return None


async def check_login_state(platform: str, headless: bool = True) -> bool | None:
    """Checks login state using the persistent profile (no seed injection).

    Opens the platform home in a throwaway persistent-context on the profile
    dir, then closes it. Never raises for page-level issues; returns None
    instead.
    """
    from playwright.async_api import async_playwright

    if platform not in PLATFORM_HOME_URL:
        return None
    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir_for(platform)),
                headless=headless,
                args=playwright_launch_args(),
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(
                    PLATFORM_HOME_URL[platform],
                    wait_until="domcontentloaded",
                    timeout=LOGIN_CHECK_TIMEOUT_MS,
                )
                await page.wait_for_timeout(2500)
                return await page_shows_logged_in(page, platform)
            finally:
                await context.close()
    except Exception as exc:
        logger.warning("Login check failed for %s: %s", platform, exc)
        return None


async def inject_seed_once(
    platform: str, cookies: list[dict] | None = None, headless: bool = True
) -> bool:
    """Injects first-boot seed cookies into the persistent profile, then re-checks.

    Must only be called when the profile shows logged-out. Returns the
    post-injection login state (True only if the re-check shows logged in).
    """
    if cookies is None:
        cookies = load_seed_cookies(platform)
    if not cookies:
        logger.warning("No usable seed cookies for platform %s", platform)
        return False
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir_for(platform)),
                headless=headless,
                args=playwright_launch_args(),
            )
            try:
                await context.add_cookies(cookies)
                logger.info("Injected %d seed cookies for %s (one-shot)", len(cookies), platform)
            finally:
                await context.close()
    except Exception as exc:
        logger.warning("Seed injection failed for %s: %s", platform, exc)
        return False
    state = await check_login_state(platform, headless=headless)
    return state is True


async def ensure_session(platform: str, headless: bool = True) -> tuple[bool, str]:
    """Guarantees a logged-in persistent profile before the agent runs.

    Returns (ok, detail). When ok is False the caller must NOT run the agent;
    surface NEEDS_HUMAN with detail instead.
    """
    if platform not in PLATFORM_HOME_URL:
        return True, f"platform '{platform}': no login check available, proceeding"

    state = await check_login_state(platform, headless=headless)
    if state is True:
        return True, "already logged in via persistent profile"

    seed_cookies = load_seed_cookies(platform) if state is False else []
    if state is False and seed_cookies:
        logger.info("Profile logged out for %s; injecting first-boot seed once", platform)
        if await inject_seed_once(platform, seed_cookies, headless=headless):
            return True, "session restored from first-boot seed"
        return False, (
            "Seed cookies did not restore a session (expired, rejected, or bound "
            "to a different IP/user-agent). Run "
            f"'python scripts/manual_login.py {platform}' on this machine to "
            "create a fresh session."
        )

    if state is False:
        return False, (
            f"Logged out of {platform} and no seed cookies found in {AUTH_STATES_DIR}. "
            f"Run 'python scripts/manual_login.py {platform}' on this machine to "
            "log in once; the persistent profile will keep the session."
        )

    logger.warning("Could not determine login state for %s; proceeding anyway", platform)
    return True, "login state unknown; proceeding"


# ---------------------------------------------------------------------------
# browser-use Browser construction (persistent profile preferred)
# ---------------------------------------------------------------------------


def _browser_init_params() -> set[str]:
    try:
        return set(inspect.signature(Browser.__init__).parameters)
    except (TypeError, ValueError):
        return set()


def _config_class_with_field(field_name: str) -> Any | None:
    """Finds a browser_use BrowserConfig/BrowserProfile class exposing field_name."""
    candidates = [
        ("browser_use", "BrowserConfig"),
        ("browser_use", "BrowserProfile"),
        ("browser_use.browser.browser", "BrowserConfig"),
        ("browser_use.browser.profile", "BrowserProfile"),
    ]
    for module_name, class_name in candidates:
        try:
            module = __import__(module_name, fromlist=[class_name])
            cls = getattr(module, class_name, None)
        except ImportError:
            continue
        if cls is None:
            continue
        try:
            params = set(inspect.signature(cls).parameters)
        except (TypeError, ValueError):
            continue
        if field_name in params:
            return cls
    return None


def _class_has_field(cls: Any, field_name: str) -> bool:
    """True when a browser config class accepts the given constructor field."""
    fields = getattr(cls, "model_fields", None)
    if isinstance(fields, dict):
        return field_name in fields
    return hasattr(cls, field_name)


def _pacing_config() -> dict[str, float] | None:
    """Human-like pacing parameters, or None when HUMAN_PACING is disabled.

    Slows the agent to human speed: a fixed delay between individual browser
    actions, a randomized pause after every agent step, and fewer actions per
    step (more LLM round-trips, each looking like one or two deliberate
    actions). This is account protection, not just politeness — rapid
    automated navigation from a datacenter IP is what gets sessions flagged.
    """
    raw = os.getenv("HUMAN_PACING")
    if raw is None:
        enabled = bool(getattr(settings, "HUMAN_PACING", True))
    else:
        enabled = raw.strip().lower() not in ("false", "0", "no", "off")
    if not enabled:
        return None

    def _num(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, "") or getattr(settings, name, default))
        except (TypeError, ValueError):
            logger.warning("Invalid %s; using default %s", name, default)
            return default

    def _int(name: str, default: int) -> int:
        try:
            return int(float(os.getenv(name, "") or getattr(settings, name, default)))
        except (TypeError, ValueError):
            logger.warning("Invalid %s; using default %s", name, default)
            return default

    pause_min = _num("HUMAN_STEP_PAUSE_MIN_S", 3.0)
    pause_max = _num("HUMAN_STEP_PAUSE_MAX_S", 7.0)
    if pause_max < pause_min:
        pause_min, pause_max = pause_max, pause_min
    return {
        "action_delay": _num("HUMAN_ACTION_DELAY_S", 2.0),
        "pause_min": pause_min,
        "pause_max": pause_max,
        "max_actions_per_step": _int("HUMAN_MAX_ACTIONS_PER_STEP", 2),
    }


def _make_step_pause_callback(pause_min: float, pause_max: float):
    """Builds the Agent's per-step callback: a randomized human-like pause."""

    async def _pause(browser_state_summary, agent_output, step_number) -> None:
        delay = random.uniform(pause_min, pause_max)
        logger.debug("human-pacing: %.1fs pause after step %s", delay, step_number)
        await asyncio.sleep(delay)

    return _pause


def build_browser(
    *,
    headless: bool,
    profile_dir: Path | None,
    storage_state: str | None = None,
    wait_between_actions: float | None = None,
) -> Browser:
    """Builds a browser-use Browser, preferring a persistent profile dir.

    Detection order:
      1. ``Browser(..., user_data_dir=...)`` kwarg (native persistent profile).
      2. ``Browser(config=BrowserConfig/BrowserProfile(user_data_dir=...))``.
      3. Fallback: Playwright ``storage_state`` seed file. NOTE — this path
         exists only for browser-use builds without persistent-profile
         support; it re-injects seed cookies on every run, which is exactly
         the staleness bug this module fixes. Prefer 1 or 2 (upgrade
         browser-use if you land here).

    ``wait_between_actions`` (human-pacing) is passed through wherever the
    browser-use build accepts it.
    """
    init_params = _browser_init_params()
    if profile_dir is not None:
        if "user_data_dir" in init_params:
            logger.info("Using persistent browser profile at %s", profile_dir)
            extra: dict[str, Any] = {}
            if wait_between_actions is not None and "wait_between_actions" in init_params:
                extra["wait_between_actions"] = wait_between_actions
            return Browser(
                headless=headless, user_data_dir=str(profile_dir), **extra
            )
        config_cls = _config_class_with_field("user_data_dir")
        if config_cls is not None:
            try:
                logger.info(
                    "Using persistent browser profile at %s (via %s)",
                    profile_dir,
                    config_cls.__name__,
                )
                cfg_kwargs: dict[str, Any] = {
                    "headless": headless,
                    "user_data_dir": str(profile_dir),
                }
                if wait_between_actions is not None and _class_has_field(
                    config_cls, "wait_between_actions"
                ):
                    cfg_kwargs["wait_between_actions"] = wait_between_actions
                return Browser(config=config_cls(**cfg_kwargs))
            except Exception as exc:
                logger.warning("Persistent-profile config failed (%s); falling back.", exc)
    if storage_state:
        logger.warning(
            "browser-use build lacks persistent-profile support; falling back to "
            "storage_state seed file %s. Sessions may go stale — upgrade browser-use.",
            storage_state,
        )
        return Browser(headless=headless, storage_state=str(storage_state))
    return Browser(headless=headless)


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------


def get_llm() -> Any:
    """Builds the browser-use chat model.

    - ``LLM_PROVIDER=gemini`` (default): Google Gemini via settings.GEMINI_API_KEY
      and settings.GEMINI_MODEL (default ``gemini-2.5-flash``).
    - ``LLM_PROVIDER=muse``: Meta Model API (OpenAI-compatible) at
      ``META_MODEL_API_BASE_URL`` (default https://api.meta.ai/v1) with model
      ``MUSE_SPARK_MODEL`` (default ``muse-spark-1.3``). Requires
      ``MODEL_API_KEY`` in the environment.
    - ``LLM_PROVIDER=auto``: quota-aware failover chain for applications —
      Groq (free) -> Gemini (free) -> Gemini paid. Free tiers are exhausted
      first; paid Gemini only engages when free quota is gone. Every failover
      is logged. Set ``APPLY_ALLOW_PAID=false`` to keep it free-only.
    """
    provider = (
        os.getenv("LLM_PROVIDER") or getattr(settings, "LLM_PROVIDER", "gemini")
    ).lower()
    if provider == "auto":
        logger.info("LLM_PROVIDER=auto: building failover chain for apply agent")
        return get_apply_llm()
    if provider == "muse":
        return _get_muse_llm()

    api_key = settings.GEMINI_API_KEY
    if api_key:
        os.environ["GOOGLE_API_KEY"] = api_key
        os.environ["GEMINI_API_KEY"] = api_key
    model = (
        getattr(settings, "GEMINI_MODEL", None)
        or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    )
    logger.info("Using Gemini model %s for browser agent", model)
    return build_gemini_llm(api_key, model)


def _get_muse_llm() -> Any:
    """Muse Spark via the Meta Model API (OpenAI-compatible endpoint)."""
    api_key = os.environ.get("MODEL_API_KEY") or os.environ.get("META_API_KEY", "")
    base_url = os.environ.get(
        "META_MODEL_API_BASE_URL",
        getattr(settings, "META_MODEL_API_BASE_URL", "https://api.meta.ai/v1"),
    )
    model = os.environ.get(
        "MUSE_SPARK_MODEL", getattr(settings, "MUSE_SPARK_MODEL", "muse-spark-1.3")
    )
    logger.info("Using Muse Spark model %s via %s", model, base_url)
    return build_openai_compat_llm(base_url, api_key, model)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _remove_legacy_merged_session() -> None:
    """Removes the legacy _active_session.json merged every run by the old loader.

    It is purely derived from seed files and is superseded by persistent
    profiles; leaving it around only confuses debugging.
    """
    legacy = AUTH_STATES_DIR / "_active_session.json"
    if legacy.exists():
        try:
            legacy.unlink()
            logger.info("Removed legacy merged session file %s", legacy.name)
        except OSError as exc:
            logger.warning("Could not remove legacy session file: %s", exc)


async def autofill_job_application(
    job_url: str,
    job_id: str,
    profile: ApplicantProfile = applicant_profile,
    resume_path: Path | str | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    platform = detect_platform(job_url)
    profile_dir = profile_dir_for(platform)
    _remove_legacy_merged_session()

    # 0. Session gate: the persistent profile must show logged-in (or be
    #    restored from the one-shot seed). Never run the agent logged-out.
    session_ok, session_detail = await ensure_session(platform, headless=headless)
    if not session_ok:
        logger.warning("Session gate failed for job_id=%s: %s", job_id, session_detail)
        return {
            "status": STATUS_NEEDS_HUMAN,
            "job_id": job_id,
            "screenshot_path": None,
            "message": session_detail,
            "failure_reason": "session_expired",
        }

    llm = get_llm()

    # Seed file for the storage_state fallback path only (first-boot seed).
    # Unused when the browser-use build supports persistent profiles.
    seed_cookies = load_seed_cookies(platform)
    seed_state_file = None
    if seed_cookies:
        seed_state_file = AUTH_STATES_DIR / f"_seed_{platform}.json"
        with open(seed_state_file, "w", encoding="utf-8") as f:
            json.dump({"cookies": seed_cookies, "origins": []}, f, indent=2)

    pacing = _pacing_config()
    if pacing is not None:
        logger.info(
            "human-pacing enabled: action_delay=%.1fs step_pause=%.1f-%.1fs "
            "max_actions_per_step=%d",
            pacing["action_delay"],
            pacing["pause_min"],
            pacing["pause_max"],
            int(pacing["max_actions_per_step"]),
        )
    else:
        logger.info("human-pacing disabled (HUMAN_PACING=false)")

    browser = build_browser(
        headless=headless,
        profile_dir=profile_dir,
        storage_state=str(seed_state_file) if seed_state_file else None,
        wait_between_actions=pacing["action_delay"] if pacing else None,
    )

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

    agent_kwargs: dict[str, Any] = {}
    if pacing is not None:
        agent_kwargs["max_actions_per_step"] = int(pacing["max_actions_per_step"])
        agent_kwargs["register_new_step_callback"] = _make_step_pause_callback(
            pacing["pause_min"], pacing["pause_max"]
        )
    agent = Agent(
        task=task_instructions,
        llm=llm,
        browser=browser,
        available_file_paths=available_files,
        **agent_kwargs,
    )

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
                    logger.info("Copied review screenshot from %s to %s", last_path, screenshot_file)
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
            # Best-effort: surface the agent's own step errors so the worker's
            # failure classifier sees the real cause (e.g. a 402 billing error)
            # instead of guessing from a generic message.
            agent_errors = []
            try:
                for step in list(getattr(history, "history", []) or [])[-3:]:
                    for action_result in getattr(step, "result", None) or []:
                        err = getattr(action_result, "error", None)
                        if err:
                            agent_errors.append(str(err))
            except Exception:
                pass
            detail = "; ".join(dict.fromkeys(agent_errors))[:500]
            message = final_summary or (
                f"Agent failed before completing the form. {detail}"
                if detail
                else "Agent failed before completing the form (no summary produced)."
            )
            return {
                "status": STATUS_FAILED,
                "job_id": job_id,
                "screenshot_path": verified_screenshot_path,
                "message": message,
            }

        return {
            "status": STATUS_PENDING_REVIEW,
            "job_id": job_id,
            "screenshot_path": verified_screenshot_path,
            "message": final_summary
            or "Application autofilled with resume attached and ready for review.",
        }
    except Exception as exc:
        logger.error("Browser agent failed on job_id=%s: %s", job_id, exc)
        return {
            "status": STATUS_FAILED,
            "job_id": job_id,
            "error": str(exc),
        }
    finally:
        await browser.close()
