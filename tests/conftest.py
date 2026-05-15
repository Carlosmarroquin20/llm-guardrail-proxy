"""Shared pytest fixtures.

The fixtures defined here are intentionally narrow. Broad, project-wide
fixtures tend to encourage hidden coupling between unrelated test modules; we
prefer module-local fixtures unless a value is genuinely cross-cutting.
"""

from __future__ import annotations

import pytest

from llm_guardrail_proxy.core import TokenomicsService


@pytest.fixture(scope="session")
def tokenomics() -> TokenomicsService:
    """Process-wide service instance.

    Reused across tests because the underlying ``tiktoken`` encoding load is
    the slowest operation in the suite; constructing a fresh service per test
    inflates wall-clock by an order of magnitude with no isolation benefit
    (the service is stateless).
    """

    return TokenomicsService(allow_unknown_models=True)


@pytest.fixture(scope="session")
def strict_tokenomics() -> TokenomicsService:
    """Variant configured to reject unknown model identifiers."""

    return TokenomicsService(allow_unknown_models=False)
