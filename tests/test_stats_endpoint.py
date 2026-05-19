"""End-to-end tests for the ``/stats/*`` router."""

from __future__ import annotations

from decimal import Decimal

import httpx

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.app import build_app
from llm_guardrail_proxy.proxy.audit import InMemoryAuditSink
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


def _build(
    *,
    upstream_handler,
    enable_stats: bool = True,
    include_secret_scan: bool = True,
):
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
    middlewares = []
    if include_secret_scan:
        middlewares.append(SecretScanMiddleware(scanner=SecretScanner()))
    middlewares.append(
        TokenomicsMiddleware(service=TokenomicsService(), policy=permissive)
    )
    pipeline = MiddlewarePipeline(middlewares)

    sink = InMemoryAuditSink(capacity=50)
    settings = ProxySettings(
        openai_base_url="https://upstream-openai.test",
        anthropic_base_url="https://upstream-anthropic.test",
        enable_stats_endpoint=enable_stats,
    )
    app = build_app(
        settings=settings,
        pipeline=pipeline,
        forwarder=forwarder,
        audit_sink=sink,
        stats_repository=sink,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    )
    return client, upstream_client, sink


async def _drive_one_allowed(client: httpx.AsyncClient) -> None:
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )


async def _drive_one_rejected(client: httpx.AsyncClient) -> None:
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "leak AKIAABCDEFGHIJKLMNOP"}
            ],
        },
    )


# --------------------------------------------------------------- mounting


class TestRouterMounting:
    async def test_router_is_mounted_when_enabled(self) -> None:
        client, upstream, _ = _build(
            upstream_handler=lambda r: httpx.Response(200, json={"id": "ok"})
        )
        try:
            response = await client.get("/stats/summary")
        finally:
            await client.aclose()
            await upstream.aclose()
        assert response.status_code == 200

    async def test_router_is_absent_when_disabled(self) -> None:
        client, upstream, _ = _build(
            upstream_handler=lambda r: httpx.Response(200, json={}),
            enable_stats=False,
        )
        try:
            response = await client.get("/stats/summary")
        finally:
            await client.aclose()
            await upstream.aclose()
        assert response.status_code == 404


# ---------------------------------------------------------------- summary


class TestSummaryEndpoint:
    async def test_empty_state_returns_zeroed_summary(self) -> None:
        client, upstream, _ = _build(
            upstream_handler=lambda r: httpx.Response(200, json={"id": "ok"})
        )
        try:
            response = await client.get("/stats/summary")
        finally:
            await client.aclose()
            await upstream.aclose()
        body = response.json()
        assert body["total_requests"] == 0
        assert body["allowed"] == 0
        assert body["rejected"] == 0
        assert body["rejection_rate"] == 0.0

    async def test_summary_reflects_traffic_history(self) -> None:
        client, upstream, _ = _build(
            upstream_handler=lambda r: httpx.Response(200, json={"id": "ok"})
        )
        try:
            await _drive_one_allowed(client)
            await _drive_one_allowed(client)
            await _drive_one_rejected(client)
            response = await client.get("/stats/summary")
        finally:
            await client.aclose()
            await upstream.aclose()

        body = response.json()
        assert body["total_requests"] == 3
        assert body["allowed"] == 2
        assert body["rejected"] == 1
        assert body["rejections_by_middleware"] == {"secret_scan": 1}
        assert body["requests_by_model"]["gpt-4o"] == 3
        # ``estimated_cost_usd`` is serialised as a JSON string (Decimal-safe).
        assert isinstance(body["total_estimated_cost_usd"], str)


# ----------------------------------------------------------------- recent


class TestRecentEndpoint:
    async def test_recent_returns_newest_first(self) -> None:
        client, upstream, _ = _build(
            upstream_handler=lambda r: httpx.Response(200, json={"id": "ok"})
        )
        try:
            await _drive_one_allowed(client)
            await _drive_one_rejected(client)
            response = await client.get("/stats/recent")
        finally:
            await client.aclose()
            await upstream.aclose()

        records = response.json()
        assert len(records) == 2
        # Newest first — the rejection is the most recent emission.
        assert records[0]["verdict"] == "rejected"
        assert records[1]["verdict"] == "allowed"

    async def test_recent_honours_limit(self) -> None:
        client, upstream, _ = _build(
            upstream_handler=lambda r: httpx.Response(200, json={"id": "ok"})
        )
        try:
            for _ in range(5):
                await _drive_one_allowed(client)
            response = await client.get("/stats/recent?limit=3")
        finally:
            await client.aclose()
            await upstream.aclose()

        records = response.json()
        assert len(records) == 3

    async def test_recent_rejects_out_of_range_limit(self) -> None:
        client, upstream, _ = _build(
            upstream_handler=lambda r: httpx.Response(200, json={})
        )
        try:
            response = await client.get("/stats/recent?limit=0")
        finally:
            await client.aclose()
            await upstream.aclose()
        # FastAPI returns 422 for query validation failures.
        assert response.status_code == 422

    async def test_recent_payload_does_not_leak_raw_secrets(self) -> None:
        client, upstream, _ = _build(
            upstream_handler=lambda r: httpx.Response(200, json={})
        )
        try:
            await _drive_one_rejected(client)
            response = await client.get("/stats/recent")
        finally:
            await client.aclose()
            await upstream.aclose()
        # The serialised body must not contain the raw AWS-shaped fixture.
        assert "AKIAABCDEFGHIJKLMNOP" not in response.text
