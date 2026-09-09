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
    # Blank means auto-detect from whichever provider's keys are present.
    llm_provider: str = ""
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
    # Base URL of the Firecrawl API. Change it for a self-hosted instance or a
    # regional endpoint. A pasted "/v1" or "/v2" suffix is tolerated.
    firecrawl_api_url: str = "https://api.firecrawl.dev"
    firecrawl_api_version: str = "v2"       # v2 (current) or v1 (legacy)

    # --- Network ---------------------------------------------------------
    # HTTPS_PROXY / HTTP_PROXY / NO_PROXY from the environment are honoured
    # automatically. PROXY_URL forces one for every outbound call, for
    # machines where the proxy is set in the OS but not exported to the shell.
    proxy_url: str | None = None
    # Path to a PEM bundle that includes your network's root certificate. Needed
    # behind a TLS-intercepting proxy (Zscaler, Netskope, corporate firewalls),
    # which otherwise fails every HTTPS call with CERTIFICATE_VERIFY_FAILED.
    # SSL_CERT_FILE and REQUESTS_CA_BUNDLE are read as well.
    ca_bundle: str | None = None
    ssl_cert_file: str | None = None
    requests_ca_bundle: str | None = None
    # Last resort only: disables certificate verification for every call.
    tls_verify: bool = True
    # Verify against the operating system's certificate store (Windows, macOS,
    # Linux) instead of Python's bundled list. This is what makes a corporate
    # proxy's root certificate, which the OS already trusts, work for Python
    # too. Needs the `truststore` package; falls back silently without it.
    use_system_certs: bool = True
    ncbi_api_key: str | None = None
    ncbi_tool: str = "celestra"
    ncbi_email: str = "research@example.org"
    icd11_client_id: str | None = None
    icd11_client_secret: str | None = None
    loinc_username: str | None = None
    loinc_password: str | None = None

    research_cutoff: str = ""  # ISO date; blank means today

    def _azure_ready(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key
                    and self.azure_openai_deployment)

    def _foundry_ready(self) -> bool:
        return bool(self.foundry_api_key and self.foundry_resource)

    @property
    def provider_is_explicit(self) -> bool:
        return bool((self.llm_provider or "").strip())

    @property
    def provider(self) -> str:
        """The provider in use.

        An explicit LLM_PROVIDER wins. Otherwise the provider is whichever one
        has a complete set of credentials, so adding Azure keys to .env is
        enough on its own; forgetting to also flip LLM_PROVIDER used to leave
        the app silently on Anthropic reporting a missing Anthropic key.
        """
        explicit = (self.llm_provider or "").strip().lower()
        if explicit:
            return explicit
        if self._azure_ready():
            return "azure_openai"
        if self._foundry_ready():
            return "anthropic_foundry"
        return "anthropic"

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
        gaps = [name for name, value in required if not value]
        if gaps and not self.provider_is_explicit and self.provider == "anthropic":
            # Nothing was configured at all; say what any provider would need
            # rather than implying Anthropic is the only option.
            return ["ANTHROPIC_API_KEY, or AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY + "
                    "AZURE_OPENAI_DEPLOYMENT, or FOUNDRY_API_KEY + FOUNDRY_RESOURCE"]
        return gaps

    @property
    def firecrawl_enabled(self) -> bool:
        return bool(self.firecrawl_api_key)

    def firecrawl_endpoint(self, action: str) -> str:
        """Full URL for a Firecrawl action, from the configured base and version."""
        base = (self.firecrawl_api_url or "https://api.firecrawl.dev").strip().rstrip("/")
        version = (self.firecrawl_api_version or "v2").strip().strip("/").lower()
        for suffix in ("/v1", "/v2"):
            if base.endswith(suffix):
                version = suffix.strip("/")
                base = base[: -len(suffix)]
        if version not in ("v1", "v2"):
            version = "v2"
        return f"{base}/{version}/{action}"

    @property
    def firecrawl_version(self) -> str:
        return self.firecrawl_endpoint("x").rsplit("/", 2)[-2]

    def tls_verify_value(self) -> bool | str:
        """What httpx should verify against: a CA bundle path, True, or False."""
        if not self.tls_verify:
            return False
        for candidate in (self.ca_bundle, self.ssl_cert_file, self.requests_ca_bundle):
            if candidate and candidate.strip():
                return candidate.strip()
        return True

    def network_status(self) -> dict[str, Any]:
        verify = self.tls_verify_value()
        return {
            "trust_store": tls_trust_description(),
            "proxy": self.proxy_url or "from environment (HTTPS_PROXY) if set",
            "proxy_forced": bool(self.proxy_url),
            "ca_bundle": verify if isinstance(verify, str) else "",
            "tls_verify": verify is not False,
            "firecrawl_endpoint": self.firecrawl_endpoint("search"),
            "firecrawl_version": self.firecrawl_version,
        }

    def llm_status(self) -> dict[str, Any]:
        """What the UI shows about the model layer."""
        return {
            "configured": self.llm_enabled,
            "provider": self.provider,
            "explicit": self.provider_is_explicit,
            "model": self.active_model,
            "gaps": self.provider_gaps(),
        }

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


_TLS_STATE: dict[str, Any] = {"configured": False, "system": False, "detail": ""}


def configure_tls() -> dict[str, Any]:
    """Make every HTTPS client in the process trust what the operating system
    trusts. Idempotent; call it before the first client is built.

    Python ships its own certificate list and ignores the OS store. On a
    machine behind a TLS-intercepting proxy the browser works (Windows trusts
    the proxy's root certificate) while Python fails every call with
    CERTIFICATE_VERIFY_FAILED. `truststore` closes that gap by routing
    verification through the OS store, which is what pip itself does.
    """
    if _TLS_STATE["configured"]:
        return _TLS_STATE
    s = get_settings()
    _TLS_STATE["configured"] = True
    verify = s.tls_verify_value()
    if verify is False:
        _TLS_STATE["detail"] = "verification disabled (TLS_VERIFY=false)"
        return _TLS_STATE
    if isinstance(verify, str):
        _TLS_STATE["detail"] = f"custom CA bundle {verify}"
        return _TLS_STATE
    if not s.use_system_certs:
        _TLS_STATE["detail"] = "Python's bundled certificates (USE_SYSTEM_CERTS=false)"
        return _TLS_STATE
    try:
        import truststore  # noqa: PLC0415

        truststore.inject_into_ssl()
        _TLS_STATE["system"] = True
        _TLS_STATE["detail"] = "operating system certificate store (truststore)"
    except ImportError:
        _TLS_STATE["detail"] = ("Python's bundled certificates; install `truststore` "
                                "(pip install -r requirements.txt) to use the OS store")
    except Exception as exc:  # noqa: BLE001 - never fail startup over this
        _TLS_STATE["detail"] = f"Python's bundled certificates (truststore failed: {exc})"
    return _TLS_STATE


def tls_trust_description() -> str:
    return str(configure_tls().get("detail") or "")


def system_certs_active() -> bool:
    return bool(configure_tls().get("system"))


def ensure_dirs() -> None:
    configure_tls()
    for path in (DATA_DIR, REFERENCE_DIR, DATA_DIR / "cache"):
        path.mkdir(parents=True, exist_ok=True)
