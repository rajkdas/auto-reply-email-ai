"""Settings loaded from environment / .env via pydantic-settings.

All secrets and tunables live here; no other module reads os.environ directly.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import os
from dotenv import load_dotenv

# Force the .env variables into the system environment
load_dotenv()

class Mode(str, Enum):
    """Operating mode for the system."""

    dry_run = "dry_run"
    draft_only = "draft_only"
    live = "live"


class SmtpSecurity(str, Enum):
    starttls = "starttls"
    ssl = "ssl"


class Settings(BaseSettings):
    """Strongly-typed configuration sourced from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Mode ----------------------------------------------------------
    mode: Mode = Mode.dry_run

    # Safety post-check that downgrades a "send" decision to "draft" when the
    # generated reply looks suspicious (needs_human flag, unauthorized URLs or
    # money amounts). Set POST_CHECK_ENABLED=false in .env to disable it and
    # let approved replies go out over SMTP unconditionally.
    post_check_enabled: bool = False

    # --- LLM -----------------------------------------------------------
    llm_provider: str = "openai"
    llm_analysis_model: str = "gpt-4o-mini"
    llm_reply_model: str = "gpt-4o-mini"
    llm_reply_fallback_model: str | None = None

    # --- Provider keys (kept as raw strings so they can be empty) ------
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str | None = None

    # --- IMAP ----------------------------------------------------------
    imap_host: str = "imap.example.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"

    # --- SMTP ----------------------------------------------------------
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_security: SmtpSecurity = SmtpSecurity.starttls
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "ReplyDesk"

    # --- Polling / workers --------------------------------------------
    poll_seconds: int = 30
    workers: int = 4
    max_concurrency: int = 4
    # --- Testing limits -------------------------------------------------
    # Max number of emails to process per ``once``/poll iteration.
    # 0 = no limit (process every unseen email). Set MAX_EMAILS=1 while
    # testing so the run stops after working on a single email.
    max_emails: int = 0

    # --- Safety knobs --------------------------------------------------
    products_file: str = "products.yaml"
    allowed_domains: str = "example.com"
    max_replies_per_sender: int = 3
    min_confidence: float = 0.7
    fuzzy_match_threshold: float = 80
    max_body_chars: int = 6000

    # --- Storage -------------------------------------------------------
    db_path: str = "data/replydesk.db"

    # --- Logging -------------------------------------------------------
    log_level: str = "INFO"

    # --- Helpers -------------------------------------------------------
    @field_validator("allowed_domains")
    @classmethod
    def _split_domains(cls, v: str) -> str:
        # Accept comma-separated list, keep as-is but strip whitespace.
        return ",".join(d.strip().lower() for d in v.split(",") if d.strip())

    @property
    def allowed_domain_list(self) -> list[str]:
        return [d for d in self.allowed_domains.split(",") if d]

    @property
    def db_path_resolved(self) -> Path:
        p = Path(self.db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def get_settings() -> Settings:
    """Factory used everywhere; cached later if needed."""
    return Settings()
