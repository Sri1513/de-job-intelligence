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

    # Human-like pacing for the browser agent (account protection: rapid
    # automated navigation from a datacenter IP is what gets sessions
    # flagged). HUMAN_PACING=false restores full speed.
    HUMAN_PACING: bool = True
    HUMAN_ACTION_DELAY_S: float = 2.0  # native wait between browser actions
    HUMAN_STEP_PAUSE_MIN_S: float = 3.0  # randomized pause after each agent step
    HUMAN_STEP_PAUSE_MAX_S: float = 7.0
    HUMAN_MAX_ACTIONS_PER_STEP: int = 2  # fewer actions/step = more human-like

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

    # Password for the read-only mcp_dashboard_reader role (migrations
    # 004/005). Lives ONLY in the VM .env; the public MCP route refuses
    # to serve data when this is empty (fail-closed). Never commit a
    # value here, and generate it with `openssl rand -hex 32` so it
    # stays URL-safe for the reader pool connection string.
    MCP_DASHBOARD_READER_PASSWORD: str = ""

    # Public base URL of the MCP server, used for OAuth 2.1 discovery
    # metadata (RFC 8414 / RFC 9728) and redirect construction.
    MCP_PUBLIC_URL: str = "https://mcp.sriomkar.com"

    # Sprint 2, Batch 4 — HTTP guards (src/protocols/guard.py). All four
    # are set above anything the owner's own clients send, so normal use
    # (Gemini app, Inspector, scripts, dashboard backend) is unaffected:
    # - MAX_REQUEST_BYTES: 1 MB. Tool calls are kilobytes; even staging
    #   100 jobs at once is a few hundred KB. Oversized bodies get 413
    #   before anything reads them.
    # - ALLOWED_ORIGINS: browser origins allowed to call the JSON-RPC
    #   surface. Only enforced when an Origin header is PRESENT (only
    #   browsers send one — server-side clients send none and pass
    #   through). Loopback origins are always allowed (local Inspector /
    #   local dev). Add a hosted Inspector's origin here if you ever
    #   connect one directly from the browser instead of via its proxy.
    # - RATE_LIMIT_PER_MINUTE: per credential (SHA-256 of the bearer
    #   token; no credential means per client IP), sliding 60s window.
    #   60/min is far above the owner's handful of calls a minute; it
    #   exists to stop one runaway client flooding the server. 0 disables.
    # - TOOL_TIMEOUT_SECONDS: per tools/call, enforced in handle_rpc
    #   where the tool name is known. run_application_worker and
    #   retry_job_evaluations are exempt (slow by nature); the ingestion
    #   / email tools return immediately so the timeout never bites them.
    MCP_MAX_REQUEST_BYTES: int = 1_048_576
    MCP_ALLOWED_ORIGINS: str = "https://jobs.sriomkar.com,https://sriomkar.com"
    MCP_RATE_LIMIT_PER_MINUTE: int = 60
    MCP_TOOL_TIMEOUT_SECONDS: float = 30.0

    # Admin API (remote diagnostics for the AI operator). When empty, the
    # /api/admin/* routes behave as if they do not exist (404).
    ADMIN_API_TOKEN: str = ""

    # Dashboard write endpoints (/api/*/update-status, /api/applications/queue,
    # ...) require a Bearer token matching ADMIN_API_TOKEN. Reads stay public.
    # Dashboard debug mode (tracebacks in responses). Keep off in production.
    DASHBOARD_DEBUG: bool = False

    # URL the dashboard's admin health check uses to probe the MCP server.
    MCP_HEALTH_URL: str = "http://localhost:8000/health"

    # "Ask Market Intelligence" drawer: the dashboard's MCP-client URL for
    # the public MCP surface. In compose the MCP server is reachable by
    # service name (see docker-compose.yml); localhost is the local-dev
    # default. No token is needed: /public/mcp is unauthenticated by
    # design and serves the reader pool only.
    MARKET_CHAT_MCP_URL: str = "http://localhost:8000/public/mcp"

    # Optional LLM synthesis for the drawer (OpenAI-compatible
    # chat-completions). Empty by default: the drawer then answers from a
    # deterministic template over the same MCP data. To enable, set the
    # URL to Open WebUI's route -- https://llm.sriomkar.com/api/chat/completions
    # -- and the KEY to an API key created in an Open WebUI account's
    # settings (not the browser session JWT, which expires). The key is a
    # service credential for that account and lives in .env only.
    MARKET_CHAT_LLM_URL: str = ""
    # Chat model for the portfolio + market drawers. At deploy time set
    # this to qwen2.5:7b so chats use the fast Windows 7B (Windows-first
    # via src/core/llm_routing.py, VM 3B as degraded fallback). The 3B
    # queue does NOT use this setting -- see LOCAL_LLM_MODEL below.
    MARKET_CHAT_LLM_MODEL: str = "qwen2.5:3b"
    MARKET_CHAT_LLM_KEY: str = ""

    # Windows Ollama (Tailscale) -- primary endpoint for the router in
    # src/core/llm_routing.py. The jobs VM and the LLM VM reach it over
    # the tailnet at this address. Ollama has no auth, so no key is ever
    # sent on this path, and no Cloudflare sits in front of it.
    WINDOWS_LLM_BASE_URL: str = "http://100.68.109.36:11434"

    # Model served by the VM's Ollama. Used by the 3B queue AND as the
    # downgrade target when a chat request for another model (e.g.
    # qwen2.5:7b) falls back to the VM. Pinned: changing it changes what
    # the queue runs on -- re-run the battery first.
    LOCAL_LLM_MODEL: str = "qwen2.5:3b"

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
