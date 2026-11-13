"""Application configuration loaded from environment variables.

Configuration and persistent data are resolved independently of the current
working directory, so the CLI, Alembic, pytest and uvicorn all see the same
.env file and the same SQLite database no matter where they are started from.

Resolution rules
----------------
1. ``MORNING_ASSISTANT_ENV_FILE`` (if set) names the .env file to load. An empty
   value means "load no .env file" (used by the test suite).
2. Otherwise ``<project root>/.env`` is used when it exists.
3. Otherwise ``<project root>/backend/.env`` is used when it exists (older setups).
4. A relative ``DATA_PATH`` is resolved against the folder that contains the
   .env file that was loaded (or the project root when no file was loaded).
"""

import os
from pathlib import Path
from typing import Optional, Tuple

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
ENV_FILE_VARIABLE = "MORNING_ASSISTANT_ENV_FILE"

# Canonical OAuth callback routes registered by the application.
GMAIL_CALLBACK_ROUTE = "/api/gmail/callback"
QUICKBOOKS_CALLBACK_ROUTE = "/api/quickbooks/callback"


def resolve_env_file() -> Tuple[Optional[Path], str]:
    """Return (env file path or None, human readable reason)."""
    explicit = os.environ.get(ENV_FILE_VARIABLE)
    if explicit is not None:
        if not explicit.strip():
            return None, f"{ENV_FILE_VARIABLE} is empty: no .env file loaded"
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path, f"from {ENV_FILE_VARIABLE}"

    root_env = PROJECT_ROOT / ".env"
    backend_env = BACKEND_DIR / ".env"
    if root_env.exists():
        return root_env, "project root .env"
    if backend_env.exists():
        return backend_env, "backend\\.env (legacy location)"
    return None, "no .env file found (using environment variables only)"


ENV_FILE, ENV_FILE_REASON = resolve_env_file()
CONFIG_BASE_DIR = ENV_FILE.parent if ENV_FILE else PROJECT_ROOT


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE else None,
        # utf-8-sig tolerates a byte-order mark written by Notepad/PowerShell.
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    # Application
    public_origin: str = "http://localhost:8000"
    session_secret: str = ""  # validated by startup_problems(); >= 32 characters
    owner_username: str = "owner"
    owner_password_hash: str = ""
    data_path: Path = Path("./data")
    token_encryption_key: str = ""
    owner_timezone: str = "America/New_York"
    debug: bool = False
    max_upload_size_mb: int = 25

    # Gmail
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_oauth_callback: str = GMAIL_CALLBACK_ROUTE
    gmail_label_name: str = "Property Demo"

    # QuickBooks
    quickbooks_environment: str = "sandbox"  # "sandbox" or "production"
    quickbooks_client_id: str = ""
    quickbooks_client_secret: str = ""
    quickbooks_oauth_callback: str = QUICKBOOKS_CALLBACK_ROUTE
    quickbooks_expected_realm_id: str = ""
    quickbooks_home_currency: str = "USD"
    quickbooks_minor_version: str = "75"

    # OpenAI
    openai_api_key: str = ""
    openai_extraction_model: str = "gpt-4o"
    openai_transcription_model: str = "whisper-1"
    openai_search_model: str = "gpt-4o-mini"

    # Todoist
    todoist_enabled: bool = False
    todoist_api_token: str = ""
    todoist_project_id: str = ""
    todoist_default_review_days: int = 3

    @field_validator("data_path", mode="after")
    @classmethod
    def _absolute_data_path(cls, value: Path) -> Path:
        value = Path(value).expanduser()
        if not value.is_absolute():
            value = CONFIG_BASE_DIR / value
        return value.resolve()

    @field_validator("gmail_oauth_callback", "quickbooks_oauth_callback", mode="after")
    @classmethod
    def _callback_path(cls, value: str) -> str:
        value = (value or "").strip()
        if value.startswith("http://") or value.startswith("https://"):
            raise ValueError(
                "OAuth callback settings must be a path such as /api/gmail/callback; "
                "the origin comes from PUBLIC_ORIGIN"
            )
        if not value.startswith("/api/"):
            raise ValueError("OAuth callback paths must start with /api/")
        return value.rstrip("/")

    @field_validator("public_origin", mode="after")
    @classmethod
    def _origin(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def env_file(self) -> Optional[Path]:
        return ENV_FILE

    @property
    def documents_path(self) -> Path:
        """Path where original documents are stored."""
        return self.data_path / "documents"

    @property
    def emails_path(self) -> Path:
        """Path where raw email sources are stored."""
        return self.data_path / "emails"

    @property
    def temp_path(self) -> Path:
        """Path for temporary uploads."""
        return self.data_path / "temp"

    @property
    def database_path(self) -> Path:
        return self.data_path / "morning_assistant.db"

    @property
    def database_url(self) -> str:
        """SQLite database URL."""
        return f"sqlite+aiosqlite:///{self.database_path.as_posix()}"

    @property
    def sync_database_url(self) -> str:
        """Synchronous SQLite database URL for Alembic."""
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def max_upload_bytes(self) -> int:
        """Maximum upload size in bytes."""
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def gmail_redirect_uri(self) -> str:
        """Exact redirect URI to register with Google and send in OAuth requests."""
        return f"{self.public_origin}{self.gmail_oauth_callback}"

    @property
    def quickbooks_redirect_uri(self) -> str:
        """Exact redirect URI to register with Intuit and send in OAuth requests."""
        return f"{self.public_origin}{self.quickbooks_oauth_callback}"

    @property
    def quickbooks_base_url(self) -> str:
        """QuickBooks API base URL based on environment."""
        if self.quickbooks_environment == "sandbox":
            return "https://sandbox-quickbooks.api.intuit.com"
        return "https://quickbooks.api.intuit.com"

    @property
    def quickbooks_app_url(self) -> str:
        """QuickBooks web app base URL for opening records."""
        if self.quickbooks_environment == "sandbox":
            return "https://app.sandbox.qbo.intuit.com"
        return "https://app.qbo.intuit.com"

    @property
    def quickbooks_auth_url(self) -> str:
        """Intuit OAuth authorization URL."""
        return "https://appcenter.intuit.com/connect/oauth2"

    @property
    def quickbooks_token_url(self) -> str:
        """Intuit OAuth token URL."""
        return "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

    def startup_problems(self) -> list:
        """Configuration errors that must stop the server from starting."""
        problems = []
        if len(self.session_secret) < 32:
            problems.append(
                "SESSION_SECRET must be at least 32 characters. "
                "Run: python backend\\cli.py generate-keys"
            )
        return problems

    def is_gmail_configured(self) -> bool:
        """Check if Gmail integration is configured."""
        return bool(self.gmail_client_id and self.gmail_client_secret)

    def is_quickbooks_configured(self) -> bool:
        """Check if QuickBooks integration is configured."""
        return bool(self.quickbooks_client_id and self.quickbooks_client_secret)

    def is_openai_configured(self) -> bool:
        """Check if OpenAI is configured."""
        return bool(self.openai_api_key)

    def is_todoist_configured(self) -> bool:
        """Check if Todoist is enabled and configured."""
        return self.todoist_enabled and bool(self.todoist_api_token and self.todoist_project_id)


# Global settings instance
settings = Settings()
