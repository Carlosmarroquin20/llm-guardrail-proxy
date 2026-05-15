"""Tests for the tokenomics middleware adapter."""

from __future__ import annotations

from decimal import Decimal

import pytest

from llm_guardrail_proxy.core import (
    ThresholdPolicy,
    TokenomicsService,
)
from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    ParsedPrompt,
    Provider,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.middlewares import TokenomicsMiddleware


def _envelope(content: str, model: str = "gpt-4o") -> ProxyRequest:
    return ProxyRequest(
        path="/v1/chat/completions",
        method="POST",
        headers={},
        raw_body=b"{}",
        parsed=ParsedPrompt(provider=Provider.OPENAI, model=model, content=content),
    )


@pytest.fixture
def service() -> TokenomicsService:
    return TokenomicsService()


class TestTokenomicsMiddleware:
    async def test_compliant_prompt_continues_with_annotations(
        self, service: TokenomicsService
    ) -> None:
        policy = ThresholdPolicy(max_tokens=1_000, max_cost_usd=Decimal("1"))
        mw = TokenomicsMiddleware(service=service, policy=policy)
        outcome = await mw.process(_envelope("hello"))
        assert isinstance(outcome, Continue)
        assert outcome.annotations["token_count"] > 0
        assert "estimated_cost_usd" in outcome.annotations
        assert outcome.annotations["fallback_applied"] is False

    async def test_overlong_prompt_is_rejected_with_413(
        self, service: TokenomicsService
    ) -> None:
        policy = ThresholdPolicy(max_tokens=1)
        mw = TokenomicsMiddleware(service=service, policy=policy)
        outcome = await mw.process(_envelope("the quick brown fox"))
        assert isinstance(outcome, Reject)
        assert outcome.status_code == 413
        assert outcome.reason == "tokenomics_policy_violation"
        assert "token_limit" in outcome.annotations["violations"]

    async def test_cost_violation_is_reported(
        self, service: TokenomicsService
    ) -> None:
        policy = ThresholdPolicy(max_cost_usd=Decimal("0.0000000001"))
        mw = TokenomicsMiddleware(service=service, policy=policy)
        outcome = await mw.process(_envelope("hello world", model="gpt-4"))
        assert isinstance(outcome, Reject)
        assert "cost_limit" in outcome.annotations["violations"]

    async def test_unknown_model_triggers_fallback_annotation(
        self, service: TokenomicsService
    ) -> None:
        policy = ThresholdPolicy(max_tokens=10_000, max_cost_usd=Decimal("10"))
        mw = TokenomicsMiddleware(service=service, policy=policy)
        outcome = await mw.process(_envelope("hi", model="future-model-7"))
        assert isinstance(outcome, Continue)
        assert outcome.annotations["fallback_applied"] is True
