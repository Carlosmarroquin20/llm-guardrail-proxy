"""FastAPI application factory.

The factory is deliberately parameterised: every collaborator (settings,
pipeline, forwarder) can be supplied externally. This is what makes the
proxy testable end-to-end without touching the network and reusable as a
library — Phase 5's pre-commit integration constructs a ``build_app``
variant with no forwarder at all.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.circuit_breaker import CircuitBreaker
from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    Provider,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.exceptions import (
    PromptExtractionError,
    ProviderResolutionError,
    UpstreamError,
)
from llm_guardrail_proxy.proxy.forwarder import UpstreamForwarder
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.middlewares import (
    SecretScanMiddleware,
    TokenomicsMiddleware,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline
from llm_guardrail_proxy.proxy.providers import resolve_adapter, supported_paths
from llm_guardrail_proxy.proxy.scanning import SecretScanner
from llm_guardrail_proxy.proxy.settings import ProxySettings

_LOGGER = logging.getLogger("llm_guardrail_proxy")


def build_app(
    *,
    settings: ProxySettings,
    pipeline: MiddlewarePipeline,
    forwarder: UpstreamForwarder,
) -> FastAPI:
    """Construct a fully-wired FastAPI application.

    The lifespan hook does not own the ``httpx.AsyncClient``: that is the
    caller's responsibility, because in tests we hand in a pre-built client
    backed by :class:`httpx.MockTransport` whose lifecycle is managed by
    the test fixture.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        _LOGGER.info(
            "llm-guardrail-proxy ready (paths=%s)", supported_paths()
        )
        yield

    app = FastAPI(
        title="llm-guardrail-proxy",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.pipeline = pipeline
    app.state.forwarder = forwarder

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def handle(request: Request) -> Response:
        return await _handle_proxied_request(request)

    # One route per supported path; this gives FastAPI accurate OpenAPI
    # metadata and avoids the catch-all wildcard that would silently swallow
    # typos in client configuration.
    for path in supported_paths():
        app.add_api_route(
            path,
            handle,
            methods=["POST"],
            include_in_schema=True,
            name=f"proxy:{path}",
        )

    return app


async def _handle_proxied_request(request: Request) -> Response:
    """Inner request handler shared by every protected route."""

    pipeline: MiddlewarePipeline = request.app.state.pipeline
    forwarder: UpstreamForwarder = request.app.state.forwarder

    raw_body = await request.body()

    try:
        adapter = resolve_adapter(request.url.path)
        parsed = adapter.parse(raw_body)
    except ProviderResolutionError as exc:
        return JSONResponse(
            status_code=404,
            content={"error": "unknown_provider", "detail": str(exc)},
        )
    except PromptExtractionError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "malformed_request", "detail": str(exc)},
        )

    envelope = ProxyRequest(
        path=request.url.path,
        method=request.method,
        headers={k: v for k, v in request.headers.items()},
        raw_body=raw_body,
        parsed=parsed,
    )

    decision = await pipeline.run(envelope)
    if isinstance(decision.outcome, Reject):
        return JSONResponse(
            status_code=decision.outcome.status_code,
            content={
                "error": decision.outcome.reason,
                "detail": decision.outcome.detail,
                "middleware": decision.rejecting_middleware,
                "annotations": decision.annotations,
            },
        )

    assert isinstance(decision.outcome, Continue)  # narrowing for mypy/readers

    try:
        return await forwarder.forward(envelope)
    except UpstreamError as exc:
        # 502 — upstream returned no usable response, or the breaker is open.
        return JSONResponse(
            status_code=502,
            content={"error": "upstream_unavailable", "detail": str(exc)},
        )


def create_default_app() -> FastAPI:
    """Build an application with production defaults.

    Intended for ``uvicorn`` direct invocation. The ``httpx.AsyncClient`` is
    constructed here because, unlike the test fixture, the production path
    has no reason to share a client outside the app lifecycle.
    """

    settings = ProxySettings()
    service = TokenomicsService(allow_unknown_models=settings.allow_unknown_models)
    policy = ThresholdPolicy(
        max_tokens=settings.max_prompt_tokens,
        max_cost_usd=settings.max_prompt_cost_usd,
    )

    # Pipeline order is significant. Secret detection runs first so that a
    # leaked credential is surfaced even when the prompt would also have
    # violated a cost ceiling: security-class verdicts outrank FinOps ones.
    middlewares: list[Middleware] = []
    if settings.enable_secret_scanning:
        middlewares.append(SecretScanMiddleware(scanner=SecretScanner()))
    middlewares.append(TokenomicsMiddleware(service=service, policy=policy))
    pipeline = MiddlewarePipeline(middlewares)

    client = httpx.AsyncClient(timeout=settings.upstream_timeout_seconds)
    breaker = CircuitBreaker(
        failure_threshold=settings.breaker_failure_threshold,
        reset_seconds=settings.breaker_reset_seconds,
    )
    forwarder = UpstreamForwarder(
        client=client,
        breaker=breaker,
        origins={
            Provider.OPENAI: str(settings.openai_base_url),
            Provider.ANTHROPIC: str(settings.anthropic_base_url),
        },
    )

    return build_app(settings=settings, pipeline=pipeline, forwarder=forwarder)
