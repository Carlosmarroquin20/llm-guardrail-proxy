"""Typed value objects exchanged between core components.

Pydantic v2 is used for runtime validation at the boundary so that any future
non-Python caller (HTTP middleware, CLI argv parsing) receives the same
guarantees the core enforces internally. ``model_config`` is configured for
immutability to ensure evaluation verdicts cannot be mutated after the fact —
a property the Phase 4 audit ledger relies on for integrity.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ViolationKind(str, Enum):
    """Discriminator for the precise rule that a prompt violated.

    Subclasses ``str`` so its members serialise transparently in JSON
    audit records without requiring a custom encoder.
    """

    TOKEN_LIMIT = "token_limit"
    COST_LIMIT = "cost_limit"


class ThresholdPolicy(BaseModel):
    """Declarative threshold configuration.

    Either bound may be omitted, in which case the corresponding check is
    skipped. At least one bound must be provided; an all-``None`` policy is
    rejected at construction time to prevent silently no-op evaluations.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tokens: Annotated[int, Field(gt=0)] | None = None
    max_cost_usd: Annotated[Decimal, Field(gt=Decimal("0"))] | None = None

    def model_post_init(self, __context: object) -> None:  # noqa: D401
        if self.max_tokens is None and self.max_cost_usd is None:
            raise ValueError(
                "ThresholdPolicy requires at least one of "
                "'max_tokens' or 'max_cost_usd'."
            )


class CostEstimate(BaseModel):
    """Result of a single tokenize + price operation.

    The ``encoding_used`` field is retained for observability: when a fallback
    encoding is applied, FinOps dashboards must be able to distinguish exact
    measurements from approximations.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    token_count: Annotated[int, Field(ge=0)]
    estimated_cost_usd: Annotated[Decimal, Field(ge=Decimal("0"))]
    encoding_used: str
    fallback_applied: bool


class EvaluationResult(BaseModel):
    """Verdict returned by ``TokenomicsService.evaluate``.

    ``violations`` is a tuple (not a list) to preserve the frozen-model
    contract: the entire object — including its collections — must be safe to
    pass across threads or store in an immutable audit log.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    estimate: CostEstimate
    policy: ThresholdPolicy
    violations: tuple[ViolationKind, ...] = ()

    @property
    def is_allowed(self) -> bool:
        """Convenience predicate for downstream middleware dispatch."""
        return not self.violations
