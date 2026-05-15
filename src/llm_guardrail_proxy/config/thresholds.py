"""Default threshold policy applied when none is supplied by the caller.

The numbers below are deliberately conservative: Phase 1 ships a guardrail,
not a permissive default. Operators are expected to override these via the
configuration plane introduced in Phase 2.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from llm_guardrail_proxy.core.models import ThresholdPolicy

# 8k tokens is a reasonable upper bound for an interactive developer prompt;
# anything substantially larger is almost always an unintended paste of source
# code or build logs and should be flagged for review.
_DEFAULT_MAX_TOKENS: Final[int] = 8_000

# USD 0.05 per request maps to roughly 16k tokens on gpt-4-turbo — well above
# normal interactive use, but low enough to catch runaway batch scripts before
# they accumulate material spend.
_DEFAULT_MAX_COST_USD: Final[Decimal] = Decimal("0.05")

DEFAULT_POLICY: Final[ThresholdPolicy] = ThresholdPolicy(
    max_tokens=_DEFAULT_MAX_TOKENS,
    max_cost_usd=_DEFAULT_MAX_COST_USD,
)
