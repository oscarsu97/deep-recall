"""Environment-backed configuration.

Every component takes a `Config` rather than reading `os.environ` directly, so
tests can build one by hand and the CLI can validate credentials up front
instead of failing halfway through an API call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when required credentials are missing or malformed."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; falling back to %d", name, raw, default)
        return default


def _env_list(name: str) -> list[str]:
    return [part.strip() for part in _env(name).split(",") if part.strip()]


@dataclass
class Config:
    # Notion
    notion_token: str = ""
    notion_database_id: str = ""
    notion_page_ids: list[str] = field(default_factory=list)
    ingest_lookback_hours: int = 24

    # LLM
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Local
    vault_dir: Path = REPO_ROOT / "vault"
    max_cards_per_session: int = 15
    git_auto_commit: bool = True

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        """Read configuration from `.env` (if present) and the process environment."""
        load_dotenv(dotenv_path=env_file or REPO_ROOT / ".env", override=False)

        vault_raw = _env("VAULT_DIR", "vault")
        vault_dir = Path(vault_raw)
        if not vault_dir.is_absolute():
            vault_dir = REPO_ROOT / vault_dir

        return cls(
            notion_token=_env("NOTION_TOKEN"),
            notion_database_id=_env("NOTION_DATABASE_ID"),
            notion_page_ids=_env_list("NOTION_PAGE_IDS"),
            ingest_lookback_hours=_env_int("INGEST_LOOKBACK_HOURS", 24),
            llm_provider=_env("LLM_PROVIDER", "gemini").lower(),
            gemini_api_key=_env("GEMINI_API_KEY"),
            gemini_model=_env("GEMINI_MODEL", "gemini-2.0-flash"),
            groq_api_key=_env("GROQ_API_KEY"),
            groq_model=_env("GROQ_MODEL", "llama-3.3-70b-versatile"),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            vault_dir=vault_dir,
            max_cards_per_session=_env_int("MAX_CARDS_PER_SESSION", 15),
            git_auto_commit=_env_bool("GIT_AUTO_COMMIT", True),
        )

    # -- validation -------------------------------------------------------
    # Each command needs a different slice of the config, so validation is
    # per-capability rather than one all-or-nothing check at startup.

    def require_notion(self) -> None:
        if not self.notion_token:
            raise ConfigError(
                "NOTION_TOKEN is not set. Create an internal integration at "
                "https://www.notion.so/my-integrations and share your notes page with it."
            )

    def require_llm(self) -> None:
        if self.llm_provider == "gemini":
            if not self.gemini_api_key:
                raise ConfigError(
                    "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey "
                    "or switch to LLM_PROVIDER=groq."
                )
        elif self.llm_provider == "groq":
            if not self.groq_api_key:
                raise ConfigError(
                    "GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys "
                    "or switch to LLM_PROVIDER=gemini."
                )
        else:
            raise ConfigError(
                f"LLM_PROVIDER={self.llm_provider!r} is not supported (use 'gemini' or 'groq')."
            )

    def require_telegram(self, need_chat_id: bool = True) -> None:
        if not self.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather.")
        if need_chat_id and not self.telegram_chat_id:
            raise ConfigError("TELEGRAM_CHAT_ID is not set. Message @userinfobot to find yours.")
