# src/core/config.py
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent

class Settings(BaseSettings):
    # Database
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "job_scout_db"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""

    # AI Engine
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.6-flash"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"

    # Google Workspace Template IDs (dual-aliased for backwards compatibility)
    RESUME_TEMPLATE_DOC_ID: str = ""
    GOOGLE_DOCS_TEMPLATE_ID: str = ""

    # Server Ports
    MCP_SERVER_PORT: int = 8000
    DASHBOARD_PORT: int = 5001

    # Directory Paths
    CONFIG_DIR: Path = BASE_DIR / "config"
    LOGS_DIR: Path = BASE_DIR / "logs"

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def get_template_id(self) -> str:
        return self.RESUME_TEMPLATE_DOC_ID or self.GOOGLE_DOCS_TEMPLATE_ID

settings = Settings()
