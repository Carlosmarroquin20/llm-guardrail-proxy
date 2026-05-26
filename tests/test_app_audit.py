"""End-to-end tests for Phase 4 audit emission.

Each test drives the ASGI app in-process and asserts that exactly one
audit record is produced per request, with the expected verdict and
correlation metadata.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import httpx

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.app import build_app
from llm_guardrail_proxy.proxy.audit import (
    EnforcementVerdict,
    InMemoryAuditSink,
)
from llm_guardrail_proxy.proxy.circuit_breaker import CircuitBreaker
from llm_guardrail_proxy.proxy.envelope import Provider
from llm_guardrail_proxy.proxy.forwarder import UpstreamForwarder
from llm_guardrail_proxy.proxy.middlewares import (
    SecretScanMiddleware,
    TokenomicsMiddleware,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline
from llm_guardrail_proxy.proxy.scanning import SecretScanner
from llm_guardrail_proxy.proxy.settings import ProxySettings


def _build(*, policy: ThresholdPolicy, upstream_handler, include_secret_scan: bool = True):
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
    middlewares = []
    if include_secret_scan:
        middlewares.append(SecretScanMiddleware(scanner=SecretScanner()))
    middlewares.append(
        TokenomicsMiddleware(service=TokenomicsService(), policy=policy)
    )
    pipeline = MiddlewarePipeline(middlewares)
    settings = ProxySettings(
        network={
            "openai_base_url": "https://upstream-openai.test",
            "anthropic_base_url": "https://upstream-anthropic.test",
        },
    )
    sink = InMemoryAuditSink(capacity=10)
    app = build_app(
        settings=settings,
        pipeline=pipeline,
        forwarder=forwarder,
        audit_sink=sink,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    )
    return client, upstream_client, sink


# ----------------------------------------------------------------- happy


class TestAuditOnSuccess:
    async def test_record_is_created_for_allowed_request(self) -> None:
        permissive = ThresholdPolicy(max_tokens=10_000, max_cost_usd=Decimal("10"))
        client, upstream, sink = _build(
            policy=permissive,
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
        records = sink.records
        assert len(records) == 1
        rec = records[0]
        assert rec.verdict is EnforcementVerdict.ALLOWED
        assert rec.upstream_status_code == 200
        assert rec.token_count is not None and rec.token_count > 0
        assert rec.estimated_cost_usd is not None
        assert rec.model == "gpt-4o"

    async def test_response_carries_correlation_header(self) -> None:
        permissive = ThresholdPolicy(max_tokens=10_000)
        client, upstream, sink = _build(
            policy=permissive,
            upstream_handler=lambda r: httpx.Response(200, json={"id": "ok"}),
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
            await upstream.aclose()

        assert "x-request-id" in {h.lower() for h in response.headers.keys()}
        header_value = response.headers["x-request-id"]
        # Header value is a parseable UUID matching the audit record.
        assert UUID(header_value) == sink.records[0].request_id


# --------------------------------------------------------------- rejection


class TestAuditOnReject:
    async def test_rejected_request_is_recorded(self) -> None:
        # Strict token policy guarantees rejection.
        strict = ThresholdPolicy(max_tokens=1)
        invocations = 0

        def upstream(_: httpx.Request) -> httpx.Response:
            nonlocal invocations
            invocations += 1
            return httpx.Response(200, json={})

        client, upstream_client, sink = _build(
            policy=strict, upstream_handler=upstream
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "user", "content": "the quick brown fox jumps over"}
                    ],
                },
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        assert response.status_code == 413
        assert invocations == 0
        records = sink.records
        assert len(records) == 1
        rec = records[0]
        assert rec.verdict is EnforcementVerdict.REJECTED
        assert rec.rejecting_middleware == "tokenomics"
        assert rec.reject_status_code == 413
        assert rec.upstream_status_code is None

    async def test_secret_finding_is_attached(self) -> None:
        permissive = ThresholdPolicy(max_tokens=10_000)
        client, upstream, sink = _build(
            policy=permissive,
            upstream_handler=lambda r: httpx.Response(200, json={}),
        )
        try:
            await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {
                            "role": "user",
                            "content": "leak: AKIAABCDEFGHIJKLMNOP",
                        }
                    ],
                },
            )
        finally:
            await client.aclose()
            await upstream.aclose()

        rec = sink.records[0]
        assert rec.verdict is EnforcementVerdict.REJECTED
        assert rec.rejecting_middleware == "secret_scan"
        assert len(rec.findings) == 1
        assert rec.findings[0].kind == "aws_access_key_id"
        # The audit record carries the redacted preview, not the secret.
        assert rec.findings[0].preview.startswith("AKIA")
        assert "AKIAABCDEFGHIJKLMNOP" not in rec.findings[0].preview


# ----------------------------------------------------------- upstream fail


class TestAuditOnUpstreamFailure:
    async def test_upstream_error_is_recorded(self) -> None:
        def upstream(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        permissive = ThresholdPolicy(max_tokens=10_000)
        client, upstream_client, sink = _build(
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
        rec = sink.records[0]
        assert rec.verdict is EnforcementVerdict.ALLOWED
        assert rec.upstream_status_code is None
        assert rec.upstream_error is not None
        assert "unreachable" in rec.upstream_error


# --------------------------------------------------------- correlation id


class TestCorrelationId:
    async def test_inbound_request_id_is_honoured(self) -> None:
        permissive = ThresholdPolicy(max_tokens=10_000)
        client, upstream, sink = _build(
            policy=permissive,
            upstream_handler=lambda r: httpx.Response(200, json={}),
        )
        supplied = "11111111-2222-3333-4444-555555555555"
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-request-id": supplied},
            )
        finally:
            await client.aclose()
            await upstream.aclose()

        assert response.headers["x-request-id"] == supplied
        assert str(sink.records[0].request_id) == supplied

    async def test_malformed_inbound_id_is_replaced(self) -> None:
        permissive = ThresholdPolicy(max_tokens=10_000)
        client, upstream, sink = _build(
            policy=permissive,
            upstream_handler=lambda r: httpx.Response(200, json={}),
        )
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-request-id": "not-a-uuid"},
            )
        finally:
            await client.aclose()
            await upstream.aclose()

        # Replaced silently — the response carries a freshly-generated UUID,
        # matching the audit record. The proxy never 400s on a bad header.
        emitted = response.headers["x-request-id"]
        UUID(emitted)  # raises if not a UUID; assertion is the absence of an exception
        assert str(sink.records[0].request_id) == emitted
