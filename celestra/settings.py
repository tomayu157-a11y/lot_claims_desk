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
    # Three providers are supported. An absent provider degrades synthesis to
    # the deterministic engine rather than failing the run.
    #   anthropic          Claude via the Anthropic API
    #   anthropic_foundry  Claude deployed on Microsoft Foundry
    #   azure_openai       a model deployed in Azure AI Foundry / Azure OpenAI
    llm_provider: str = "anthropic"
    llm_max_tokens: int = 8000
    llm_timeout_seconds: int = 120
    llm_max_concurrency: int = 4
    llm_effort: str = "high"          # low | medium | high | xhigh | max

    # Anthropic API
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    llm_model: str = "claude-opus-5"

    # Claude on Microsoft Foundry
    foundry_api_key: str | None = None
    foundry_resource: str | None = None
    foundry_model: str | None = None   # defaults to llm_model

    # Azure AI Foundry / Azure OpenAI deployment
    azure_openai_endpoint: str | None = None     # https://<name>.openai.azure.com
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str | None = None   # the deployment name, not the model
    azure_openai_api_version: str = "2024-10-21"

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
    def provider(self) -> str:
        return (self.llm_provider or "anthropic").strip().lower()

    @property
    def llm_enabled(self) -> bool:
        """Whether the selected provider has everything it needs to be called."""
        if self.provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.provider == "anthropic_foundry":
            return bool(self.foundry_api_key and self.foundry_resource)
        if self.provider == "azure_openai":
            return bool(
                self.azure_openai_endpoint
                and self.azure_openai_api_key
                and self.azure_openai_deployment
            )
        return False

    @property
    def active_model(self) -> str:
        """The model or deployment name the selected provider will call."""
        if self.provider == "azure_openai":
            return self.azure_openai_deployment or "(no deployment configured)"
        if self.provider == "anthropic_foundry":
            return self.foundry_model or self.llm_model
        return self.llm_model

    def provider_gaps(self) -> list[str]:
        """Settings the selected provider still needs. Empty when it is ready."""
        required = {
            "anthropic": [("ANTHROPIC_API_KEY", self.anthropic_api_key)],
            "anthropic_foundry": [
                ("FOUNDRY_API_KEY", self.foundry_api_key),
                ("FOUNDRY_RESOURCE", self.foundry_resource),
            ],
            "azure_openai": [
                ("AZURE_OPENAI_ENDPOINT", self.azure_openai_endpoint),
                ("AZURE_OPENAI_API_KEY", self.azure_openai_api_key),
                ("AZURE_OPENAI_DEPLOYMENT", self.azure_openai_deployment),
            ],
        }.get(self.provider)
        if required is None:
            return [f"LLM_PROVIDER '{self.llm_provider}' is not a supported provider"]
        return [name for name, value in required if not value]

    @property
    def firecrawl_enabled(self) -> bool:
        return bool(self.firecrawl_api_key)

    def credential_status(self) -> dict[str, bool]:
        return {
            "llm": self.llm_enabled,
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
