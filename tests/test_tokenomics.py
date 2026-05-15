"""Unit tests for the Phase 1 tokenomics service.

The suite is organised by behaviour, not by method name, so that future
refactors of the public surface do not invalidate the test layout. Each test
asserts a single observable property — multi-assertion tests are reserved for
cases where the assertions are intrinsically coupled (e.g. the components of
a single ``CostEstimate``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from llm_guardrail_proxy.core import (
    CostEstimate,
    EvaluationResult,
    ThresholdPolicy,
    TokenomicsService,
    ViolationKind,
)
from llm_guardrail_proxy.core.exceptions import (
    PricingError,
    ThresholdViolationError,
    TokenizationError,
)
from llm_guardrail_proxy.core.pricing import FALLBACK_ENCODING, MODEL_PRICING


# --------------------------------------------------------------------- counting


class TestCountTokens:
    """Behaviour of ``TokenomicsService.count_tokens``."""

    @pytest.mark.parametrize(
        "model",
        ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo"],
    )
    def test_known_models_produce_positive_counts(
        self,
        tokenomics: TokenomicsService,
        model: str,
    ) -> None:
        count = tokenomics.count_tokens("The quick brown fox jumps.", model)
        assert count > 0

    def test_empty_prompt_yields_zero(self, tokenomics: TokenomicsService) -> None:
        assert tokenomics.count_tokens("", "gpt-4o") == 0

    def test_unicode_prompt_is_handled(self, tokenomics: TokenomicsService) -> None:
        # Multi-byte UTF-8 input historically tripped naïve tokenizer wrappers;
        # the regression guard here is the absence of an exception.
        assert tokenomics.count_tokens("こんにちは、世界 🌐", "gpt-4o") > 0

    def test_special_token_literal_is_not_disallowed(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        # Prompts often contain the literal substring '<|endoftext|>' from
        # copy-pasted documentation. The service must encode it as text.
        assert tokenomics.count_tokens("prefix <|endoftext|> suffix", "gpt-4o") > 0

    def test_non_string_prompt_raises_tokenization_error(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        with pytest.raises(TokenizationError):
            tokenomics.count_tokens(b"bytes are not strings", "gpt-4o")  # type: ignore[arg-type]

    def test_large_prompt_scales_linearly_in_token_count(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        unit = "word " * 1_000
        single = tokenomics.count_tokens(unit, "gpt-4o")
        double = tokenomics.count_tokens(unit * 2, "gpt-4o")
        # Equality is not guaranteed because BPE merges across boundaries can
        # introduce a small variance, so a tolerance window is used.
        assert abs(double - 2 * single) <= 2


# ----------------------------------------------------------------------- cost


class TestEstimateCost:
    """Behaviour of ``TokenomicsService.estimate_cost``."""

    def test_estimate_matches_manual_calculation(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        prompt = "Audit this prompt for cost."
        estimate = tokenomics.estimate_cost(prompt, "gpt-4o")
        expected = MODEL_PRICING["gpt-4o"].input_usd_per_token * Decimal(
            estimate.token_count
        )
        assert estimate.estimated_cost_usd == expected

    def test_estimate_returns_costestimate_instance(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        estimate = tokenomics.estimate_cost("hello", "gpt-4o")
        assert isinstance(estimate, CostEstimate)
        assert estimate.fallback_applied is False
        assert estimate.encoding_used == MODEL_PRICING["gpt-4o"].tokenizer_encoding

    def test_empty_prompt_costs_zero(self, tokenomics: TokenomicsService) -> None:
        estimate = tokenomics.estimate_cost("", "gpt-4o")
        assert estimate.token_count == 0
        assert estimate.estimated_cost_usd == Decimal("0")

    def test_unknown_model_triggers_fallback(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        estimate = tokenomics.estimate_cost("hello", "gpt-9-imaginary")
        assert estimate.fallback_applied is True
        assert estimate.encoding_used == FALLBACK_ENCODING

    def test_unknown_model_in_strict_mode_raises(
        self,
        strict_tokenomics: TokenomicsService,
    ) -> None:
        with pytest.raises(PricingError):
            strict_tokenomics.estimate_cost("hello", "gpt-9-imaginary")

    @pytest.mark.parametrize(
        "model_input,expected_key",
        [
            ("GPT-4O", "gpt-4o"),
            ("  gpt-4o  ", "gpt-4o"),
            ("Gpt-3.5-Turbo", "gpt-3.5-turbo"),
        ],
    )
    def test_model_identifier_is_normalised(
        self,
        tokenomics: TokenomicsService,
        model_input: str,
        expected_key: str,
    ) -> None:
        estimate = tokenomics.estimate_cost("hello", model_input)
        assert estimate.fallback_applied is False
        assert estimate.encoding_used == MODEL_PRICING[expected_key].tokenizer_encoding

    @pytest.mark.parametrize("bad_model", ["", "   ", None])
    def test_invalid_model_raises_pricing_error(
        self,
        tokenomics: TokenomicsService,
        bad_model: object,
    ) -> None:
        with pytest.raises(PricingError):
            tokenomics.estimate_cost("hello", bad_model)  # type: ignore[arg-type]


# --------------------------------------------------------------- threshold eval


class TestEvaluate:
    """Behaviour of ``TokenomicsService.evaluate`` against ``ThresholdPolicy``."""

    def test_compliant_prompt_yields_no_violations(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        policy = ThresholdPolicy(max_tokens=1_000, max_cost_usd=Decimal("1"))
        result = tokenomics.evaluate("short prompt", "gpt-4o", policy)
        assert isinstance(result, EvaluationResult)
        assert result.is_allowed
        assert result.violations == ()

    def test_token_overflow_is_detected(self, tokenomics: TokenomicsService) -> None:
        policy = ThresholdPolicy(max_tokens=2)
        result = tokenomics.evaluate("one two three four five", "gpt-4o", policy)
        assert ViolationKind.TOKEN_LIMIT in result.violations
        assert not result.is_allowed

    def test_cost_overflow_is_detected(self, tokenomics: TokenomicsService) -> None:
        # An absurdly small cost ceiling forces any non-empty prompt over the limit.
        policy = ThresholdPolicy(max_cost_usd=Decimal("0.0000000001"))
        result = tokenomics.evaluate("hello world", "gpt-4", policy)
        assert ViolationKind.COST_LIMIT in result.violations

    def test_both_dimensions_can_violate_simultaneously(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        policy = ThresholdPolicy(
            max_tokens=1,
            max_cost_usd=Decimal("0.0000000001"),
        )
        result = tokenomics.evaluate("the quick brown fox", "gpt-4", policy)
        assert set(result.violations) == {
            ViolationKind.TOKEN_LIMIT,
            ViolationKind.COST_LIMIT,
        }

    def test_exact_token_boundary_is_inclusive(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        # Boundary semantics: the policy bound is the maximum *allowed* value,
        # so equality must not be flagged as a violation.
        prompt = "Audit this prompt for cost."
        exact = tokenomics.count_tokens(prompt, "gpt-4o")
        policy = ThresholdPolicy(max_tokens=exact)
        result = tokenomics.evaluate(prompt, "gpt-4o", policy)
        assert result.is_allowed

    def test_one_over_token_boundary_violates(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        prompt = "Audit this prompt for cost."
        exact = tokenomics.count_tokens(prompt, "gpt-4o")
        policy = ThresholdPolicy(max_tokens=exact - 1)
        result = tokenomics.evaluate(prompt, "gpt-4o", policy)
        assert ViolationKind.TOKEN_LIMIT in result.violations

    def test_policy_with_only_cost_skips_token_check(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        # A prompt that would explode any reasonable token limit must still
        # pass when the policy only constrains dollars.
        policy = ThresholdPolicy(max_cost_usd=Decimal("100"))
        result = tokenomics.evaluate("token " * 5_000, "gpt-4o", policy)
        assert result.is_allowed

    def test_empty_policy_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ThresholdPolicy()


# ----------------------------------------------------------------- enforcement


class TestEnforce:
    """Behaviour of ``TokenomicsService.enforce`` — exception-driven variant."""

    def test_compliant_prompt_returns_result(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        policy = ThresholdPolicy(max_tokens=1_000)
        result = tokenomics.enforce("hello", "gpt-4o", policy)
        assert result.is_allowed

    def test_violation_raises_with_attached_result(
        self,
        tokenomics: TokenomicsService,
    ) -> None:
        policy = ThresholdPolicy(max_tokens=1)
        with pytest.raises(ThresholdViolationError) as exc_info:
            tokenomics.enforce("a slightly longer prompt", "gpt-4o", policy)

        attached = exc_info.value.result
        assert isinstance(attached, EvaluationResult)
        assert ViolationKind.TOKEN_LIMIT in attached.violations
