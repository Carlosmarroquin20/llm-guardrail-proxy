"""Environment-driven configuration for the proxy.

``pydantic-settings`` is used so that every knob is documented in code,
validated at startup, and overridable via either environment variables or a
``.env`` file. All variables share the ``GUARDRAIL_`` prefix to prevent
collisions with the upstream provider SDKs' own configuration.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProxySettings(BaseSettings):
    """Runtime configuration object.

    Construction is lazy: callers should obtain an instance via
    :func:`get_settings` so the same validated object is shared across the
    FastAPI application lifetime instead of being rebuilt per request.
    """

    model_config = SettingsConfigDict(
        env_prefix="GUARDRAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Upstream targets ------------------------------------------------

    openai_base_url: HttpUrl = Field(
        default=HttpUrl("https://api.openai.com"),
        description="Origin used for OpenAI-shaped requests.",
    )
    anthropic_base_url: HttpUrl = Field(
        default=HttpUrl("https://api.anthropic.com"),
        description="Origin used for Anthropic-shaped requests.",
    )

    # --- Network behaviour ----------------------------------------------

    upstream_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    listen_host: str = "127.0.0.1"
    listen_port: Annotated[int, Field(gt=0, lt=65_536)] = 8080

    # --- Circuit breaker -------------------------------------------------

    breaker_failure_threshold: Annotated[int, Field(ge=1)] = 5
    breaker_reset_seconds: Annotated[float, Field(gt=0)] = 30.0

    # --- Tokenomics policy (Phase 1 reused) -----------------------------

    max_prompt_tokens: Annotated[int, Field(gt=0)] = 8_000
    max_prompt_cost_usd: Annotated[Decimal, Field(gt=Decimal("0"))] = Decimal("0.05")
    allow_unknown_models: bool = True


def get_settings() -> ProxySettings:
    """Return a freshly-validated settings instance.

    Intentionally not memoised at module level: tests routinely need to
    rebuild the object with a patched environment, and the cost of
    re-validation is negligible relative to per-request work.
    """

    return ProxySettings()
