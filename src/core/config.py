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
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"

    # Browser Automation Engine
    HEADLESS: bool = True
    CHROME_PATH: str = "/usr/bin/chromium"

    # Google Workspace Template IDs
    RESUME_TEMPLATE_DOC_ID: str = ""
    GOOGLE_DOCS_TEMPLATE_ID: str = ""

    # Server Ports
    MCP_SERVER_PORT: int = 8000
    DASHBOARD_PORT: int = 5001

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
