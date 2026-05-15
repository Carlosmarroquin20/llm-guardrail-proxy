"""End-to-end tests for the FastAPI proxy application.

Every test drives the ASGI app in-process through ``httpx.ASGITransport`` and
intercepts upstream traffic with ``httpx.MockTransport``. No socket is ever
opened — the suite is deterministic, fast, and safe to run in any CI
environment without outbound network policy.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.app import build_app
from llm_guardrail_proxy.proxy.circuit_breaker import CircuitBreaker
from llm_guardrail_proxy.proxy.envelope import Provider
from llm_guardrail_proxy.proxy.forwarder import UpstreamForwarder
from llm_guardrail_proxy.proxy.middlewares import TokenomicsMiddleware
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline
from llm_guardrail_proxy.proxy.settings import ProxySettings


def _settings() -> ProxySettings:
    return ProxySettings(
        openai_base_url="https://upstream-openai.test",
        anthropic_base_url="https://upstream-anthropic.test",
    )


def _build(
    *,
    policy: ThresholdPolicy,
    upstream_handler,
) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    """Construct the proxy app and a client targeting it.

    Returns the test client (used by the test) and the upstream-mock client
    (held by the forwarder) so the test can dispose of both deterministically.
    """

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    forwarder = UpstreamForwarder(
        client=upstream_client,
        breaker=CircuitBreaker(failure_threshold=2, reset_seconds=60),
        origins={
            Provider.OPENAI: "https://upstream-openai.test",
            Provider.ANTHROPIC: "https://upstream-anthropic.test",
        },
    )
    pipeline = MiddlewarePipeline(
        [
            TokenomicsMiddleware(
                service=TokenomicsService(),
                policy=policy,
            )
        ]
    )
    app = build_app(settings=_settings(), pipeline=pipeline, forwarder=forwarder)

    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    )
    return test_client, upstream_client


# ----------------------------------------------------------------- happy path


class TestHappyPath:
    async def test_compliant_openai_request_is_forwarded(self) -> None:
        captured: dict[str, object] = {}

        def upstream(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"id": "cmpl-1", "choices": []},
            )

        permissive = ThresholdPolicy(max_tokens=10_000, max_cost_usd=Decimal("10"))
        client, upstream_client = _build(
            policy=permissive, upstream_handler=upstream
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
            await upstream_client.aclose()

        assert response.status_code == 200
        assert response.json() == {"id": "cmpl-1", "choices": []}
        assert captured["url"] == "https://upstream-openai.test/v1/chat/completions"
        assert captured["body"]["model"] == "gpt-4o"

    async def test_anthropic_request_is_routed_to_anthropic_origin(self) -> None:
        seen: dict[str, str] = {}

        def upstream(request: httpx.Request) -> httpx.Response:
            seen["host"] = request.url.host
            return httpx.Response(200, json={"id": "msg_1"})

        permissive = ThresholdPolicy(max_tokens=10_000, max_cost_usd=Decimal("10"))
        client, upstream_client = _build(
            policy=permissive, upstream_handler=upstream
        )

        try:
            response = await client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet-latest",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 200
        assert seen["host"] == "upstream-anthropic.test"


# ------------------------------------------------------------------ rejection


class TestPolicyRejection:
    async def test_oversized_prompt_returns_413_without_calling_upstream(self) -> None:
        upstream_invocations = 0

        def upstream(_: httpx.Request) -> httpx.Response:
            nonlocal upstream_invocations
            upstream_invocations += 1
            return httpx.Response(200, json={})

        strict = ThresholdPolicy(max_tokens=1)
        client, upstream_client = _build(policy=strict, upstream_handler=upstream)

        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "user", "content": "the quick brown fox jumps"}
                    ],
                },
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 413
        body = response.json()
        assert body["error"] == "tokenomics_policy_violation"
        assert body["middleware"] == "tokenomics"
        assert upstream_invocations == 0


# ------------------------------------------------------------------ errors


class TestErrorPaths:
    async def test_unknown_path_returns_404(self) -> None:
        # The proxy does not register routes for unsupported paths, so
        # FastAPI itself answers with 404 — verifying that no opaque
        # passthrough is happening at the framework layer.
        permissive = ThresholdPolicy(max_tokens=10_000)
        client, upstream_client = _build(
            policy=permissive,
            upstream_handler=lambda r: httpx.Response(200),
        )
        try:
            response = await client.post("/v1/audio/transcriptions", json={})
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 404

    async def test_malformed_body_returns_400(self) -> None:
        permissive = ThresholdPolicy(max_tokens=10_000)
        client, upstream_client = _build(
            policy=permissive,
            upstream_handler=lambda r: httpx.Response(200),
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                content=b"not json",
                headers={"content-type": "application/json"},
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 400
        assert response.json()["error"] == "malformed_request"

    async def test_upstream_failure_returns_502(self) -> None:
        def upstream(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("upstream unreachable")

        permissive = ThresholdPolicy(max_tokens=10_000, max_cost_usd=Decimal("10"))
        client, upstream_client = _build(
            policy=permissive, upstream_handler=upstream
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 502
        assert response.json()["error"] == "upstream_unavailable"


# ------------------------------------------------------------------ health


class TestHealth:
    async def test_healthz_responds_ok(self) -> None:
        permissive = ThresholdPolicy(max_tokens=10)
        client, upstream_client = _build(
            policy=permissive,
            upstream_handler=lambda r: httpx.Response(200),
        )
        try:
            response = await client.get("/healthz")
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "model,prompt,allowed",
    [
        ("gpt-4o", "tiny", True),
        ("gpt-4o", "word " * 5_000, False),  # well above 8k tokens? no — still ok
    ],
)
async def test_parameterised_smoke(model: str, prompt: str, allowed: bool) -> None:
    """Lightweight smoke check that the full stack threads parameters end-to-end.

    Not exhaustive — the per-component suites cover behaviour; this case is
    here to catch wiring regressions that escape unit-level isolation.
    """

    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    # Pick a deliberately small threshold so the second parameter case trips it.
    policy = ThresholdPolicy(max_tokens=100)
    client, upstream_client = _build(policy=policy, upstream_handler=upstream)
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    if allowed:
        assert response.status_code == 200
    else:
        assert response.status_code == 413
