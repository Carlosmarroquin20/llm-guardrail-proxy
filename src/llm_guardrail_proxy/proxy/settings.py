"""Environment-driven configuration for the proxy.

Settings are organised into sub-models by concern. The top-level
:class:`ProxySettings` carries no leaf fields of its own — it composes
specialised models so the surface stays navigable as the project grows.

Environment variables follow the ``GUARDRAIL_<group>__<field>``
convention: the double-underscore delimiter is the pydantic-settings
default for nested resolution. Example:

::

    GUARDRAIL_NETWORK__LISTEN_PORT=8080
    GUARDRAIL_AUDIT__JSONL_PATH=./var/audit.jsonl
    GUARDRAIL_SCANNING__ENABLE_PII=true

The grouping mirrors the package layout (``audit/``, ``stats/``,
``scanning/``, etc.); navigating ``settings.audit.*`` matches the mental
model an operator already has for the code itself.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------- groups


class NetworkSettings(BaseModel):
    """Listening socket and upstream origins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    listen_host: str = "127.0.0.1"
    listen_port: Annotated[int, Field(gt=0, lt=65_536)] = 8080

    openai_base_url: HttpUrl = Field(default=HttpUrl("https://api.openai.com"))
    anthropic_base_url: HttpUrl = Field(
        default=HttpUrl("https://api.anthropic.com")
    )

    upstream_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0


class BreakerSettings(BaseModel):
    """Async circuit breaker tuning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_threshold: Annotated[int, Field(ge=1)] = 5
    reset_seconds: Annotated[float, Field(gt=0)] = 30.0


class TokenomicsSettings(BaseModel):
    """Phase 1 cost policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_prompt_tokens: Annotated[int, Field(gt=0)] = 8_000
    max_prompt_cost_usd: Annotated[Decimal, Field(gt=Decimal("0"))] = Decimal("0.05")
    allow_unknown_models: bool = True


class ScanningSettings(BaseModel):
    """Phase 3 content guardrails (secret + PII)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Secrets are regex-only — there is no performance reason to disable
    # them in normal operation, hence default-on.
    enable_secrets: bool = True

    # PII is opt-in because Presidio + spaCy + the language model add
    # ~80 MB to the install and a non-trivial cold-start cost.
    enable_pii: bool = False
    # ``block`` refuses to forward when PII is present; ``redact`` rewrites
    # the prompt in place. ``block`` is the safer default because
    # redaction can lose meaning the user needed the model to see.
    pii_policy: str = "block"
    pii_score_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5


class AuditSettings(BaseModel):
    """FinOps audit plane configuration.

    The in-memory ring is always present when ``enabled`` is True — it
    backs the stats endpoint. Additional destinations (JSONL, DuckDB,
    structlog) are layered on top via a composite sink.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    memory_capacity: Annotated[int, Field(gt=0)] = 1_000

    # ``None`` keeps the corresponding sink out of the composite.
    jsonl_path: str | None = None
    duckdb_path: str | None = None

    # When True, a LoggingAuditSink is part of the composite. The
    # structlog stack itself is configured via ``LoggingSettings``.
    log_enabled: bool = True


class StatsSettings(BaseModel):
    """Read-only ``/stats/*`` surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Default-on because the proxy binds to localhost; operators exposing
    # the proxy externally should set this to false (or front it with
    # auth) since /stats surfaces findings previews and cost data.
    enable_endpoint: bool = True
    # The HTML dashboard at /stats/dashboard. Strictly subordinate to
    # ``enable_endpoint`` — turning the endpoints off also disables the
    # dashboard regardless of this value.
    enable_dashboard: bool = True


class LoggingSettings(BaseModel):
    """Structlog stack configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``json`` is the production renderer; ``console`` is the colourised
    # interactive one for development.
    format: str = "json"
    level: str = "INFO"


# ---------------------------------------------------------------- root


class ProxySettings(BaseSettings):
    """Composite settings root.

    Construction is lazy: callers should obtain an instance via
    :func:`get_settings` so the same validated object is shared across
    the FastAPI application lifetime instead of being rebuilt per
    request.
    """

    model_config = SettingsConfigDict(
        env_prefix="GUARDRAIL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    network: NetworkSettings = Field(default_factory=NetworkSettings)
    breaker: BreakerSettings = Field(default_factory=BreakerSettings)
    tokenomics: TokenomicsSettings = Field(default_factory=TokenomicsSettings)
    scanning: ScanningSettings = Field(default_factory=ScanningSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    stats: StatsSettings = Field(default_factory=StatsSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def get_settings() -> ProxySettings:
    """Return a freshly-validated settings instance.

    Intentionally not memoised at module level: tests routinely need to
    rebuild the object with a patched environment, and the cost of
    re-validation is negligible relative to per-request work.
    """

    return ProxySettings()
