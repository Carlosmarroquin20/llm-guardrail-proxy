"""Static price matrix for supported LLM endpoints.

Values are encoded as ``Decimal`` rather than ``float`` because monetary
accumulation across millions of tokens accumulates non-trivial binary-floating
rounding error, which corrupts downstream FinOps aggregations (Phase 4).

Prices are expressed in **USD per single token** and reflect the public list
prices for the OpenAI Chat Completions / Responses API. They are intentionally
co-located in the package — Phase 4 will hydrate this matrix from a versioned
JSON manifest, but Phase 1 keeps the dependency surface zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from llm_guardrail_proxy.core.exceptions import PricingError


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Per-token list price for a single model identifier.

    Attributes
    ----------
    input_usd_per_token:
        Cost applied to prompt (request) tokens.
    output_usd_per_token:
        Cost applied to completion (response) tokens. Retained even though
        Phase 1 only estimates prompt cost, so that Phase 2's response
        middleware can reuse this structure unchanged.
    tokenizer_encoding:
        Canonical ``tiktoken`` encoding name. Stored alongside price so that
        the tokenomics service can resolve encoding and pricing in a single
        lookup, eliminating the risk of the two falling out of sync.
    """

    input_usd_per_token: Decimal
    output_usd_per_token: Decimal
    tokenizer_encoding: str


# ``MappingProxyType`` makes the public matrix read-only at runtime, protecting
# against accidental mutation by downstream middleware that may hold a reference
# to the table during request processing.
_MODEL_PRICING: dict[str, ModelPricing] = {
    "gpt-4o": ModelPricing(
        input_usd_per_token=Decimal("0.0000025"),
        output_usd_per_token=Decimal("0.000010"),
        tokenizer_encoding="o200k_base",
    ),
    "gpt-4o-mini": ModelPricing(
        input_usd_per_token=Decimal("0.00000015"),
        output_usd_per_token=Decimal("0.0000006"),
        tokenizer_encoding="o200k_base",
    ),
    "gpt-4-turbo": ModelPricing(
        input_usd_per_token=Decimal("0.000010"),
        output_usd_per_token=Decimal("0.000030"),
        tokenizer_encoding="cl100k_base",
    ),
    "gpt-4": ModelPricing(
        input_usd_per_token=Decimal("0.000030"),
        output_usd_per_token=Decimal("0.000060"),
        tokenizer_encoding="cl100k_base",
    ),
    "gpt-3.5-turbo": ModelPricing(
        input_usd_per_token=Decimal("0.0000005"),
        output_usd_per_token=Decimal("0.0000015"),
        tokenizer_encoding="cl100k_base",
    ),
}

MODEL_PRICING: Mapping[str, ModelPricing] = MappingProxyType(_MODEL_PRICING)

# ``cl100k_base`` is selected as the fallback encoding because it covers the
# majority of currently-shipping OpenAI models. Cost fallback intentionally
# adopts the most expensive listed input rate so an unknown model can never be
# silently under-priced — a conservative bias that protects FinOps budgets.
FALLBACK_ENCODING: str = "cl100k_base"
FALLBACK_PRICING: ModelPricing = ModelPricing(
    input_usd_per_token=Decimal("0.000030"),
    output_usd_per_token=Decimal("0.000060"),
    tokenizer_encoding=FALLBACK_ENCODING,
)


def resolve_pricing(model: str, *, allow_fallback: bool = True) -> ModelPricing:
    """Return the pricing entry for ``model``.

    Parameters
    ----------
    model:
        Provider-issued model identifier. Compared case-insensitively after
        stripping surrounding whitespace, since SDK clients are inconsistent
        about both.
    allow_fallback:
        When ``True`` (default), unknown models resolve to
        :data:`FALLBACK_PRICING`. When ``False``, an unknown model triggers
        :class:`PricingError`. Strict mode is intended for CI gates that should
        refuse to estimate against undocumented models.

    Raises
    ------
    PricingError
        If ``model`` is empty, or if ``allow_fallback`` is disabled and the
        identifier is not present in :data:`MODEL_PRICING`.
    """

    if not isinstance(model, str) or not model.strip():
        raise PricingError("Model identifier must be a non-empty string.")

    key = model.strip().lower()
    entry = _MODEL_PRICING.get(key)
    if entry is not None:
        return entry

    if not allow_fallback:
        raise PricingError(f"No pricing entry registered for model '{model}'.")

    return FALLBACK_PRICING
