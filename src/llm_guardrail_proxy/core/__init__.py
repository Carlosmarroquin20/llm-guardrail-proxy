"""Core domain layer.

Modules under this package are deliberately free of I/O and framework
dependencies so they remain trivially testable and reusable from any future
transport surface (HTTP middleware, CLI, pre-commit hook).
"""

from llm_guardrail_proxy.core.exceptions import (
    GuardrailError,
    PricingError,
    ThresholdViolationError,
    TokenizationError,
)
from llm_guardrail_proxy.core.models import (
    CostEstimate,
    EvaluationResult,
    ThresholdPolicy,
    ViolationKind,
)
from llm_guardrail_proxy.core.tokenomics import TokenomicsService

__all__ = [
    "CostEstimate",
    "EvaluationResult",
    "GuardrailError",
    "PricingError",
    "ThresholdPolicy",
    "ThresholdViolationError",
    "TokenizationError",
    "TokenomicsService",
    "ViolationKind",
]
