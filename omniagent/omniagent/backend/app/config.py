"""Central configuration. Secrets come from the environment / .env only."""
from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
DOMAINS_DIR = BACKEND_DIR / "domains"
SQL_DIR = BACKEND_DIR / "sql"

# Make .env values visible through os.environ so that db_connections.password_env
# (an *environment variable name*, never a password) can be resolved at connect time.
load_dotenv(BACKEND_DIR / ".env", override=False)

log = logging.getLogger("omniagent")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    secret_key: SecretStr = SecretStr("")
    app_timezone: str = "UTC"
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # Gemini
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: int = 30
    agent_max_tool_calls: int = 6
    agent_total_timeout_seconds: int = 75

    # Application database
    app_db_url: str = ""  # optional full override (used by tests)
    app_db_host: str = "127.0.0.1"
    app_db_port: int = 3306
    app_db_name: str = "omniagent_app"
    app_db_user: str = "omniagent_app"
    app_db_password: SecretStr = SecretStr("")

    # Domain databases (read-only account; password is read via password_env)
    domain_db_host: str = "127.0.0.1"
    domain_db_port: int = 3306
    domain_db_readonly_user: str = "omniagent_ro"

    # Access control
    allow_registration: bool = True
    default_domain_grants: str = ""
    session_ttl_hours: int = 12
    auth_attempts_per_minute: int = 8
    chat_requests_per_minute: int = 20
    cookie_name: str = "omniagent_session"
    trust_proxy_headers: bool = False

    # Query limits
    query_timeout_ms: int = 5000
    default_row_limit: int = 25
    max_row_limit: int = 200
    llm_max_rows: int = 30
    history_messages: int = 12
    schema_cache_ttl_seconds: int = 600
    max_message_chars: int = 2000

    # RAG (optional)
    rag_enabled: bool = False
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 768
    max_upload_mb: int = 10
    rag_chunk_chars: int = 900
    rag_chunk_overlap: int = 150
    rag_top_k: int = 5

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()}

    @property
    def default_grants(self) -> list[str]:
        return [g.strip() for g in self.default_domain_grants.split(",") if g.strip()]

    @property
    def signing_key(self) -> bytes:
        return self.secret_key.get_secret_value().encode()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.secret_key.get_secret_value():
        if s.is_production:
            raise RuntimeError("SECRET_KEY must be set in production.")
        log.warning("SECRET_KEY is empty; using an ephemeral key. Sessions reset on restart.")
        s.secret_key = SecretStr(secrets.token_urlsafe(48))
    return s
