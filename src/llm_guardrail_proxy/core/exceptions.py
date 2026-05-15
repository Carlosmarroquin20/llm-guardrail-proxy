"""Domain-specific exception hierarchy.

Every fault originating inside the guardrail core is mapped onto a subclass of
``GuardrailError``. This allows higher-level transports (the Phase 2 proxy
middleware, the Phase 5 CLI) to treat all internally-recoverable failures with
a single ``except GuardrailError`` clause while preserving granular semantics
for callers that care.
"""

from __future__ import annotations


class GuardrailError(Exception):
    """Base class for all guardrail-internal failures.

    Catching this type at the transport boundary guarantees that an unexpected
    pricing or tokenization fault never crashes the proxy worker process.
    """


class TokenizationError(GuardrailError):
    """Raised when the underlying tokenizer cannot encode the supplied prompt.

    The root cause is preserved via ``__cause__`` chaining so that observability
    pipelines (Phase 4) can surface the original ``tiktoken`` error without
    losing the context boundary established here.
    """


class PricingError(GuardrailError):
    """Raised when a cost estimate cannot be produced for the requested model.

    Distinct from ``TokenizationError`` because pricing failures are
    configuration-driven (missing entry in the matrix) whereas tokenization
    failures are typically input- or encoding-driven.
    """


class ThresholdViolationError(GuardrailError):
    """Raised by enforcement helpers when a prompt breaches a configured limit.

    The evaluation path itself does *not* raise; it returns a structured
    verdict (see :class:`llm_guardrail_proxy.core.models.EvaluationResult`).
    This exception exists for callers that prefer an exception-driven flow,
    e.g. a synchronous pre-commit hook that should exit non-zero on violation.
    """

    def __init__(self, message: str, *, result: object) -> None:
        super().__init__(message)
        # ``result`` is typed as ``object`` here to avoid an import cycle with
        # ``models``; callers narrow it to ``EvaluationResult`` at the use site.
        self.result = result
