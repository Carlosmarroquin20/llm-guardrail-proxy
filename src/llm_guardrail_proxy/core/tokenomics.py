"""Tokenomics service — Phase 1's central primitive.

This module is intentionally synchronous. The underlying ``tiktoken`` BPE call
is CPU-bound and releases the GIL only briefly; wrapping it in ``async def``
would create the illusion of concurrency without delivering any. When the
Phase 2 proxy needs to invoke this service from an async handler it should
offload via ``anyio.to_thread.run_sync`` so the event loop remains responsive.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Final

import tiktoken

from llm_guardrail_proxy.core.exceptions import (
    ThresholdViolationError,
    TokenizationError,
)
from llm_guardrail_proxy.core.models import (
    CostEstimate,
    EvaluationResult,
    ThresholdPolicy,
    ViolationKind,
)
from llm_guardrail_proxy.core.pricing import (
    FALLBACK_ENCODING,
    MODEL_PRICING,
    ModelPricing,
    resolve_pricing,
)

# A modest cache eliminates the (non-trivial) cost of re-loading BPE merge
# tables on every request, while bounding memory growth in long-lived workers.
_ENCODING_CACHE_SIZE: Final[int] = 16


@lru_cache(maxsize=_ENCODING_CACHE_SIZE)
def _load_encoding(name: str) -> tiktoken.Encoding:
    """Return a cached ``tiktoken`` encoding by canonical name.

    Encodings are immutable and thread-safe, so a process-wide cache is sound.
    The cache also serves as a circuit-breaker against pathological input that
    might otherwise trigger repeated network-backed downloads on first use.
    """

    try:
        return tiktoken.get_encoding(name)
    except (KeyError, ValueError) as exc:
        raise TokenizationError(
            f"Encoding '{name}' is not registered with tiktoken."
        ) from exc


class TokenomicsService:
    """Compute token counts, dollar estimates, and threshold verdicts.

    The service is stateless beyond its constructor parameters and is therefore
    safe to instantiate once at process start and share across request workers.

    Parameters
    ----------
    allow_unknown_models:
        When ``True`` (default), an unrecognised model identifier degrades to
        :data:`FALLBACK_ENCODING` and the conservative fallback price band.
        When ``False``, unknown models raise :class:`PricingError`, which is
        the recommended mode for CI gates.
    """

    __slots__ = ("_allow_unknown_models",)

    def __init__(self, *, allow_unknown_models: bool = True) -> None:
        self._allow_unknown_models = allow_unknown_models

    # ------------------------------------------------------------------ token

    def count_tokens(self, prompt: str, model: str) -> int:
        """Return the exact token count for ``prompt`` under ``model``'s encoding.

        An empty string is a legal input and yields ``0`` tokens. This avoids
        forcing callers to special-case empty payloads that may legitimately
        arise from upstream redaction middleware.
        """

        if not isinstance(prompt, str):
            raise TokenizationError(
                "Prompt must be a string; received "
                f"{type(prompt).__name__}."
            )

        pricing = self._resolve(model)
        encoding = self._safe_get_encoding(pricing)

        try:
            # ``disallowed_special=()`` permits special tokens to appear as
            # literal text rather than raising — important because user prompts
            # routinely embed substrings like '<|endoftext|>' from copy-paste.
            return len(encoding.encode(prompt, disallowed_special=()))
        except Exception as exc:  # tiktoken does not expose a typed hierarchy
            raise TokenizationError(
                "tiktoken failed to encode the supplied prompt."
            ) from exc

    # ------------------------------------------------------------------ cost

    def estimate_cost(self, prompt: str, model: str) -> CostEstimate:
        """Tokenize ``prompt`` and project the resulting prompt-side dollar cost."""

        pricing = self._resolve(model)
        fallback_applied = model.strip().lower() not in MODEL_PRICING
        token_count = self.count_tokens(prompt, model)

        # ``Decimal * int`` preserves precision; conversion via ``str`` is
        # unnecessary here because the price factor is already a ``Decimal``.
        cost = pricing.input_usd_per_token * Decimal(token_count)

        return CostEstimate(
            model=model,
            token_count=token_count,
            estimated_cost_usd=cost,
            encoding_used=pricing.tokenizer_encoding,
            fallback_applied=fallback_applied,
        )

    # ------------------------------------------------------------- evaluate

    def evaluate(
        self,
        prompt: str,
        model: str,
        policy: ThresholdPolicy,
    ) -> EvaluationResult:
        """Produce a structured pass/fail verdict against ``policy``.

        The method does not raise on violation: returning a value forces
        callers to make an explicit dispatch decision (allow, redact, deny,
        audit), which is the contract every later middleware phase consumes.
        """

        estimate = self.estimate_cost(prompt, model)
        violations: list[ViolationKind] = []

        if (
            policy.max_tokens is not None
            and estimate.token_count > policy.max_tokens
        ):
            violations.append(ViolationKind.TOKEN_LIMIT)

        if (
            policy.max_cost_usd is not None
            and estimate.estimated_cost_usd > policy.max_cost_usd
        ):
            violations.append(ViolationKind.COST_LIMIT)

        return EvaluationResult(
            estimate=estimate,
            policy=policy,
            violations=tuple(violations),
        )

    def enforce(
        self,
        prompt: str,
        model: str,
        policy: ThresholdPolicy,
    ) -> EvaluationResult:
        """Evaluate and raise on violation.

        Provided for synchronous integrations (pre-commit hook, CLI) that map
        violations onto a non-zero exit status; the async middleware path
        consumes :meth:`evaluate` directly.
        """

        result = self.evaluate(prompt, model, policy)
        if not result.is_allowed:
            raise ThresholdViolationError(
                f"Prompt violates policy: {', '.join(v.value for v in result.violations)}.",
                result=result,
            )
        return result

    # --------------------------------------------------------------- helpers

    def _resolve(self, model: str) -> ModelPricing:
        return resolve_pricing(model, allow_fallback=self._allow_unknown_models)

    @staticmethod
    def _safe_get_encoding(pricing: ModelPricing) -> tiktoken.Encoding:
        """Return the configured encoding, falling back if its name is unknown.

        This guards against a pricing-matrix entry that references an
        encoding the installed ``tiktoken`` version does not yet ship — a
        realistic scenario when a new model is added ahead of a library bump.
        """

        try:
            return _load_encoding(pricing.tokenizer_encoding)
        except TokenizationError:
            return _load_encoding(FALLBACK_ENCODING)
