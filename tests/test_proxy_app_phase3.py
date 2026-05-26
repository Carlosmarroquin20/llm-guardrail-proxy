"""End-to-end tests for the Phase 3 wiring.

Drives the full ASGI stack with both the secret-scanning and tokenomics
middlewares active, verifying that a leaked credential is intercepted
before any upstream call is attempted.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.app import build_app
from llm_guardrail_proxy.proxy.circuit_breaker import CircuitBreaker
from llm_guardrail_proxy.proxy.envelope import Provider
from llm_guardrail_proxy.proxy.forwarder import UpstreamForwarder
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.middlewares import (
    SecretScanMiddleware,
    TokenomicsMiddleware,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline
from llm_guardrail_proxy.proxy.scanning import SecretScanner
from llm_guardrail_proxy.proxy.settings import ProxySettings


def _build_with_secret_scanning(
    *, upstream_handler, secret_first: bool = True
) -> tuple[httpx.AsyncClient, httpx.AsyncClient, dict[str, int]]:
    """Wire a proxy with both Phase 2 and Phase 3 middlewares enabled.

    Returns the test client, the upstream-mock client (for orderly
    teardown), and a counter capturing how many times the upstream was
    invoked — the centrepiece assertion in the Phase 3 suite.
    """

    invocations = {"count": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        invocations["count"] += 1
        return upstream_handler(request)

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(counting_handler)
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
    secret_mw = SecretScanMiddleware(scanner=SecretScanner())
    tokens_mw = TokenomicsMiddleware(
        service=TokenomicsService(), policy=permissive
    )
    chain: list[Middleware] = (
        [secret_mw, tokens_mw] if secret_first else [tokens_mw, secret_mw]
    )
    pipeline = MiddlewarePipeline(chain)

    settings = ProxySettings(
        network={
            "openai_base_url": "https://upstream-openai.test",
            "anthropic_base_url": "https://upstream-anthropic.test",
        },
    )
    app = build_app(settings=settings, pipeline=pipeline, forwarder=forwarder)
    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    )
    return test_client, upstream_client, invocations


class TestSecretInterception:
    async def test_leaked_aws_key_is_rejected_before_upstream_is_called(self) -> None:
        client, upstream, invocations = _build_with_secret_scanning(
            upstream_handler=lambda r: httpx.Response(200, json={"ok": True}),
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {
                            "role": "user",
                            "content": "fix this code: const KEY = 'AKIAABCDEFGHIJKLMNOP'",
                        }
                    ],
                },
            )
        finally:
            await client.aclose()
            await upstream.aclose()

        assert response.status_code == 403
        body = response.json()
        assert body["error"] == "secret_exposure_detected"
        assert body["middleware"] == "secret_scan"
        assert invocations["count"] == 0, (
            "Upstream must never be contacted when a secret is detected."
        )

    async def test_clean_prompt_is_forwarded(self) -> None:
        client, upstream, invocations = _build_with_secret_scanning(
            upstream_handler=lambda r: httpx.Response(200, json={"id": "ok"}),
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        finally:
            await client.aclose()
            await upstream.aclose()

        assert response.status_code == 200
        assert invocations["count"] == 1


class TestMiddlewareOrdering:
    async def test_security_wins_over_finops_when_both_would_reject(self) -> None:
        # The prompt is both very long (tripping a strict token policy) AND
        # contains a leaked secret. Secret-first ordering must surface the
        # security verdict, not the FinOps one.
        invocations = {"count": 0}

        def upstream(request: httpx.Request) -> httpx.Response:
            invocations["count"] += 1
            return httpx.Response(200, json={})

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        forwarder = UpstreamForwarder(
            client=upstream_client,
            breaker=CircuitBreaker(failure_threshold=2, reset_seconds=60),
            origins={
                Provider.OPENAI: "https://upstream-openai.test",
                Provider.ANTHROPIC: "https://upstream-anthropic.test",
            },
        )
        strict_tokens = ThresholdPolicy(max_tokens=1)
        pipeline = MiddlewarePipeline(
            [
                SecretScanMiddleware(scanner=SecretScanner()),
                TokenomicsMiddleware(
                    service=TokenomicsService(), policy=strict_tokens
                ),
            ]
        )
        settings = ProxySettings(
            network={
                "openai_base_url": "https://upstream-openai.test",
                "anthropic_base_url": "https://upstream-anthropic.test",
            },
        )
        app = build_app(settings=settings, pipeline=pipeline, forwarder=forwarder)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "huge prompt " * 200
                                + " AKIAABCDEFGHIJKLMNOP"
                            ),
                        }
                    ],
                },
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        body = response.json()
        assert response.status_code == 403
        assert body["error"] == "secret_exposure_detected"
        assert invocations["count"] == 0
