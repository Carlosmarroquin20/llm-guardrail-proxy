"""Ordered middleware orchestrator.

The pipeline is intentionally minimal: no parallel fan-out, no retries, no
rewrites beyond the ``Mutate`` outcome introduced in Phase 3b. Later phases
that need fan-out (e.g. running PII analysis in parallel with secret
scanning) will introduce a dedicated parallel-stage primitive without
changing the sequential one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    Mutate,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.providers import resolve_adapter


@dataclass(frozen=True, slots=True)
class PipelineDecision:
    """Aggregate result of running every middleware against a request.

    A *passing* decision still carries the per-middleware annotations because
    Phase 4's audit ledger records cost estimates for every accepted call,
    not only for rejections.

    ``final_request`` is the envelope produced by the chain — it equals the
    input envelope when no middleware mutated the request, and the rewritten
    envelope otherwise. The route handler forwards ``final_request``, never
    the input directly, so PII redactions reach the upstream.
    """

    outcome: MiddlewareOutcome
    rejecting_middleware: str | None
    final_request: ProxyRequest
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def is_allowed(self) -> bool:
        # ``Mutate`` is an allowing outcome at the per-middleware level, but
        # the pipeline collapses it back to ``Continue`` once applied, so a
        # ``Reject`` is the only blocking case observable here.
        return not isinstance(self.outcome, Reject)


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
        """Execute the chain, short-circuiting on the first ``Reject``.

        On ``Mutate`` the pipeline rewrites the in-flight envelope via the
        provider adapter and threads the rewritten envelope into the next
        middleware. The original envelope is never mutated — every step
        produces a fresh frozen instance.
        """

        aggregated: dict[str, dict[str, Any]] = {}
        current = request

        for mw in self._middlewares:
            outcome = await mw.process(current)
            aggregated[mw.name] = dict(outcome.annotations)

            if isinstance(outcome, Reject):
                return PipelineDecision(
                    outcome=outcome,
                    rejecting_middleware=mw.name,
                    final_request=current,
                    annotations=aggregated,
                )

            if isinstance(outcome, Mutate):
                current = _apply_mutate(current, outcome)
                continue

            # ``Continue``: nothing to do.

        return PipelineDecision(
            outcome=Continue(),
            rejecting_middleware=None,
            final_request=current,
            annotations=aggregated,
        )


def _apply_mutate(request: ProxyRequest, outcome: Mutate) -> ProxyRequest:
    """Produce a rewritten envelope from a ``Mutate`` outcome.

    Adapter resolution is path-based and deterministic, so it is safe to do
    here rather than threading the adapter through the envelope. Re-parsing
    the redacted body guarantees that ``parsed.content`` reflects the bytes
    the forwarder will actually ship.
    """

    if not outcome.replacements:
        return request

    adapter = resolve_adapter(request.path)
    new_body = adapter.redact(request.raw_body, outcome.replacements)
    new_parsed = adapter.parse(new_body)
    return ProxyRequest(
        path=request.path,
        method=request.method,
        headers=request.headers,
        raw_body=new_body,
        parsed=new_parsed,
        metadata=request.metadata,
    )
