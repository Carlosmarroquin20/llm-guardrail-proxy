"""End-to-end tests for the Phase 3b PII wiring.

Skipped automatically when Presidio or its spaCy model is not installed.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

pytest.importorskip("presidio_analyzer")

try:
    import spacy  # type: ignore[import-untyped]

    spacy.load("en_core_web_sm")
except Exception:  # pragma: no cover
    pytest.skip(
        "spaCy model 'en_core_web_sm' is not installed.",
        allow_module_level=True,
    )

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.app import build_app
from llm_guardrail_proxy.proxy.circuit_breaker import CircuitBreaker
from llm_guardrail_proxy.proxy.envelope import Provider
from llm_guardrail_proxy.proxy.forwarder import UpstreamForwarder
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.middlewares import (
    PiiPolicy,
    PiiScanMiddleware,
    TokenomicsMiddleware,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline
from llm_guardrail_proxy.proxy.scanning import PiiScanner
from llm_guardrail_proxy.proxy.settings import ProxySettings


@pytest.fixture(scope="module")
def scanner() -> PiiScanner:
    return PiiScanner()


def _build(*, policy: PiiPolicy, scanner: PiiScanner, upstream_handler):
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler)
    )
    forwarder = UpstreamForwarder(
        client=upstream_client,
        breaker=CircuitBreaker(failure_threshold=2, reset_seconds=60),
        origins={
            Provider.OPENAI: "https://upstream-openai.test",
            Provider.ANTHROPIC: "https://upstream-anthropic.test",
        },
    )
    permissive = ThresholdPolicy(max_tokens=10_000, max_cost_usd=Decimal("10"))
    middlewares: list[Middleware] = [
        PiiScanMiddleware(scanner=scanner, policy=policy),
        TokenomicsMiddleware(service=TokenomicsService(), policy=permissive),
    ]
    pipeline = MiddlewarePipeline(middlewares)
    settings = ProxySettings(
        openai_base_url="https://upstream-openai.test",
        anthropic_base_url="https://upstream-anthropic.test",
    )
    app = build_app(settings=settings, pipeline=pipeline, forwarder=forwarder)
    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    )
    return test_client, upstream_client


class TestBlockMode:
    async def test_email_in_prompt_returns_403(self, scanner: PiiScanner) -> None:
        upstream_calls = 0

        def upstream(_: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return httpx.Response(200, json={"id": "ok"})

        client, upstream_client = _build(
            policy=PiiPolicy.BLOCK, scanner=scanner, upstream_handler=upstream
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "user", "content": "ping me at a@b.com"}
                    ],
                },
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 403
        assert response.json()["error"] == "pii_exposure_detected"
        assert upstream_calls == 0


class TestRedactMode:
    async def test_email_is_redacted_before_upstream(self, scanner: PiiScanner) -> None:
        captured: dict[str, object] = {}

        def upstream(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"id": "ok"})

        client, upstream_client = _build(
            policy=PiiPolicy.REDACT, scanner=scanner, upstream_handler=upstream
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "user", "content": "contact: secret@example.com"}
                    ],
                },
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 200
        message = captured["body"]["messages"][0]["content"]
        assert "secret@example.com" not in message
        assert "[REDACTED:EMAIL_ADDRESS]" in message
