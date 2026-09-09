"""Application settings. Every secret is read from the environment or .env."""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
REFERENCE_DIR = DATA_DIR / "reference"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR.parent / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Celestra"
    environment: str = "production"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = f"sqlite:///{DATA_DIR / 'celestra.db'}"

    # --- LLM -----------------------------------------------------------
    # Absent key degrades synthesis to the deterministic engine rather than failing.
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    llm_model: str = "claude-sonnet-5"
    llm_max_tokens: int = 8000
    llm_timeout_seconds: int = 120
    llm_max_concurrency: int = 4

    # --- Retrieval credentials ------------------------------------------
    firecrawl_api_key: str | None = None
    ncbi_api_key: str | None = None
    ncbi_tool: str = "celestra"
    ncbi_email: str = "research@example.org"
    icd11_client_id: str | None = None
    icd11_client_secret: str | None = None
    loinc_username: str | None = None
    loinc_password: str | None = None

    research_cutoff: str = ""  # ISO date; blank means today

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def firecrawl_enabled(self) -> bool:
        return bool(self.firecrawl_api_key)

    def credential_status(self) -> dict[str, bool]:
        return {
            "anthropic_api_key": bool(self.anthropic_api_key),
            "firecrawl_api_key": bool(self.firecrawl_api_key),
            "ncbi_api_key": bool(self.ncbi_api_key),
            "icd11": bool(self.icd11_client_id and self.icd11_client_secret),
            "loinc": bool(self.loinc_username and self.loinc_password),
        }


def _load_yaml(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@functools.lru_cache(maxsize=1)
def get_framework() -> dict[str, Any]:
    return _load_yaml("framework.yaml")


@functools.lru_cache(maxsize=1)
def get_questions() -> dict[str, Any]:
    return _load_yaml("research_questions.yaml")


@functools.lru_cache(maxsize=1)
def get_source_registry() -> dict[str, Any]:
    return _load_yaml("sources.yaml")


@functools.lru_cache(maxsize=1)
def get_thresholds() -> dict[str, Any]:
    return _load_yaml("thresholds.yaml")


def ensure_dirs() -> None:
    for path in (DATA_DIR, REFERENCE_DIR, DATA_DIR / "cache"):
        path.mkdir(parents=True, exist_ok=True)
