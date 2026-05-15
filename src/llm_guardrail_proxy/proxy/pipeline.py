"""Ordered middleware orchestrator.

The pipeline is intentionally minimal: no parallel fan-out, no retries, no
rewrites. Phase 2 establishes the contract; later phases that need fan-out
(e.g. running PII analysis in parallel with secret scanning) will introduce
a dedicated parallel-stage primitive without changing the sequential one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.middleware import Middleware


@dataclass(frozen=True, slots=True)
class PipelineDecision:
    """Aggregate result of running every middleware against a request.

    A *passing* decision still carries the per-middleware annotations because
    Phase 4's audit ledger records cost estimates for every accepted call,
    not only for rejections.
    """

    outcome: MiddlewareOutcome
    rejecting_middleware: str | None
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def is_allowed(self) -> bool:
        return isinstance(self.outcome, Continue)


class MiddlewarePipeline:
    """Run an immutable, ordered chain of middlewares against a request.

    The pipeline copies its input sequence at construction time so callers
    cannot mutate the chain after the fact — a property the Phase 4 audit
    plane will rely on to assert that a recorded outcome reflects the exact
    middleware order that produced it.
    """

    __slots__ = ("_middlewares",)

    def __init__(self, middlewares: Iterable[Middleware]) -> None:
        chain: tuple[Middleware, ...] = tuple(middlewares)
        seen: set[str] = set()
        for mw in chain:
            if mw.name in seen:
                raise ValueError(
                    f"Duplicate middleware name '{mw.name}' in pipeline."
                )
            seen.add(mw.name)
        self._middlewares: tuple[Middleware, ...] = chain

    @property
    def middlewares(self) -> Sequence[Middleware]:
        return self._middlewares

    async def run(self, request: ProxyRequest) -> PipelineDecision:
        """Execute the chain, short-circuiting on the first ``Reject``."""

        aggregated: dict[str, dict[str, Any]] = {}
        for mw in self._middlewares:
            outcome = await mw.process(request)
            aggregated[mw.name] = dict(outcome.annotations)

            if isinstance(outcome, Reject):
                return PipelineDecision(
                    outcome=outcome,
                    rejecting_middleware=mw.name,
                    annotations=aggregated,
                )

        return PipelineDecision(
            outcome=Continue(),
            rejecting_middleware=None,
            annotations=aggregated,
        )
