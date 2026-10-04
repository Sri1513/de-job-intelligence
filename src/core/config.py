# src/core/config.py
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    # Database
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "job_scout_db"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""

    # AI Engine (accepts either GEMINI_API_KEY or GOOGLE_API_KEY from .env)
    GEMINI_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )
    GEMINI_MODEL: str = "gemini-2.5-flash"
    # Paid-tier Gemini key pool (GEMINI_PAID_API_KEY, GEMINI_PAID_API_KEY_2..N,
    # or comma-separated GEMINI_PAID_API_KEYS). Used ONLY as the last resort
    # of the apply-agent failover chain (LLM_PROVIDER=auto); the analysis
    # pipeline (fit score / sponsorship) never touches paid keys.
    GEMINI_PAID_API_KEY: str = ""
    GEMINI_PAID_MODEL: str = "gemini-2.5-flash"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"

    # LLM provider for the browser agent: "gemini" (default), "muse", or "auto"
    # ("muse" = Meta Model API, OpenAI-compatible; requires MODEL_API_KEY)
    # ("auto" = quota-aware failover: Groq free -> Gemini free -> Gemini paid;
    #  set APPLY_ALLOW_PAID=false to keep the chain free-only)
    LLM_PROVIDER: str = "gemini"
    META_MODEL_API_BASE_URL: str = "https://api.meta.ai/v1"
    MUSE_SPARK_MODEL: str = "muse-spark-1.3"

    # Browser Automation Engine
    HEADLESS: bool = True
    CHROME_PATH: str = "/usr/bin/chromium"

    # Google Workspace Template IDs
    RESUME_TEMPLATE_DOC_ID: str = ""
    GOOGLE_DOCS_TEMPLATE_ID: str = ""

    # Server Ports
    MCP_SERVER_PORT: int = 8000
    DASHBOARD_PORT: int = 5001

    # MCP server authentication (RFC 6750 bearer token). The MCP server
    # refuses to start when this is empty -- set a strong random value
    # in .env, e.g. the output of `openssl rand -hex 32`.
    MCP_AUTH_TOKEN: str = ""

    # Public base URL of the MCP server, used for OAuth 2.1 discovery
    # metadata (RFC 8414 / RFC 9728) and redirect construction.
    MCP_PUBLIC_URL: str = "https://mcp.sriomkar.com"

    # Admin API (remote diagnostics for the AI operator). When empty, the
    # /api/admin/* routes behave as if they do not exist (404).
    ADMIN_API_TOKEN: str = ""

    # URL the dashboard's admin health check uses to probe the MCP server.
    MCP_HEALTH_URL: str = "http://localhost:8000/health"

    # Directory Paths
    CONFIG_DIR: Path = BASE_DIR / "config"
    LOGS_DIR: Path = BASE_DIR / "logs"
    DATA_DIR: Path = BASE_DIR / "data"
    AUTH_STATES_DIR: Path = BASE_DIR / "config" / "auth_states"
    RESUMES_DIR: Path = BASE_DIR / "config" / "resumes"
    SCREENSHOTS_DIR: Path = BASE_DIR / "data" / "screenshots"

    # Active profile selection
    PROFILE_NAME: str = "sri_omkar"
    PROFILE_PATH: Path | None = None

    @property
    def active_profile_path(self) -> Path:
        if self.PROFILE_PATH:
            return Path(self.PROFILE_PATH)
        return BASE_DIR / "config" / "profiles" / f"{self.PROFILE_NAME}.json"

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def get_template_id(self) -> str:
        return self.RESUME_TEMPLATE_DOC_ID or self.GOOGLE_DOCS_TEMPLATE_ID


settings = Settings()
