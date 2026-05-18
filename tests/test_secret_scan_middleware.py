"""Tests for the secret-scanning middleware adapter."""

from __future__ import annotations

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    ParsedPrompt,
    Provider,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.middlewares import SecretScanMiddleware
from llm_guardrail_proxy.proxy.scanning import SecretScanner


def _envelope(content: str) -> ProxyRequest:
    return ProxyRequest(
        path="/v1/chat/completions",
        method="POST",
        headers={},
        raw_body=b"{}",
        parsed=ParsedPrompt(provider=Provider.OPENAI, model="gpt-4o", content=content),
    )


class TestSecretScanMiddleware:
    middleware = SecretScanMiddleware(scanner=SecretScanner())

    async def test_clean_prompt_continues_with_zero_count(self) -> None:
        outcome = await self.middleware.process(_envelope("how do I write a unit test?"))
        assert isinstance(outcome, Continue)
        assert outcome.annotations["finding_count"] == 0

    async def test_aws_key_is_rejected_with_403(self) -> None:
        outcome = await self.middleware.process(
            _envelope("debug this: AKIAABCDEFGHIJKLMNOP")
        )
        assert isinstance(outcome, Reject)
        assert outcome.status_code == 403
        assert outcome.reason == "secret_exposure_detected"
        assert outcome.annotations["finding_count"] == 1

    async def test_finding_payload_omits_raw_secret(self) -> None:
        secret = "AKIAABCDEFGHIJKLMNOP"
        outcome = await self.middleware.process(_envelope(secret))
        assert isinstance(outcome, Reject)
        serialised = outcome.annotations["findings"][0]
        # ``preview`` is retained; the full secret must not appear anywhere
        # in the serialised payload that may be logged downstream.
        for value in serialised.values():
            assert secret not in str(value)
        assert serialised["preview"].startswith("AKIA")
        assert serialised["severity"] == "high"

    async def test_multiple_secrets_are_aggregated(self) -> None:
        text = (
            "first AKIAABCDEFGHIJKLMNOP "
            f"second ghp_{'a' * 36}"
        )
        outcome = await self.middleware.process(_envelope(text))
        assert isinstance(outcome, Reject)
        assert outcome.annotations["finding_count"] == 2
        kinds = {f["kind"] for f in outcome.annotations["findings"]}
        assert {"aws_access_key_id", "github_pat_classic"} <= kinds
