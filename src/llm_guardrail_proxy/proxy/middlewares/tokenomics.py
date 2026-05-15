"""Tokenomics middleware — Phase 1 service exposed as a pipeline link.

The middleware is a thin orchestration layer: all numerical and policy
logic lives in :mod:`llm_guardrail_proxy.core`. Keeping the middleware
shallow ensures that the Phase 1 tests already cover the math, and this
module only needs to verify the translation between proxy envelope and
core service.
"""

from __future__ import annotations

from dataclasses import dataclass

import anyio

from llm_guardrail_proxy.core import (
    EvaluationResult,
    ThresholdPolicy,
    TokenomicsService,
)
from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    ProxyRequest,
    Reject,
)


@dataclass(frozen=True, slots=True)
class TokenomicsMiddleware:
    """Reject requests that exceed configured token or cost thresholds.

    The underlying :class:`TokenomicsService` is synchronous and CPU-bound;
    invocation is therefore offloaded via ``anyio.to_thread.run_sync`` so a
    long prompt cannot stall the FastAPI event loop. The offload is cheap
    relative to a network round-trip and is the recommended approach for
    interacting with the Phase 1 core from async surfaces.
    """

    service: TokenomicsService
    policy: ThresholdPolicy
    name: str = "tokenomics"

    async def process(self, request: ProxyRequest) -> MiddlewareOutcome:
        result: EvaluationResult = await anyio.to_thread.run_sync(
            self.service.evaluate,
            request.parsed.content,
            request.parsed.model,
            self.policy,
        )

        annotations = {
            "token_count": result.estimate.token_count,
            "estimated_cost_usd": str(result.estimate.estimated_cost_usd),
            "encoding_used": result.estimate.encoding_used,
            "fallback_applied": result.estimate.fallback_applied,
        }

        if result.is_allowed:
            return Continue(annotations=annotations)

        return Reject(
            status_code=413,  # Payload Too Large — semantically correct for
                              # both token and cost overruns of a single request.
            reason="tokenomics_policy_violation",
            detail="Prompt violates configured token or cost threshold: "
            + ", ".join(v.value for v in result.violations),
            annotations={
                **annotations,
                "violations": [v.value for v in result.violations],
            },
        )
