"""FastAPI application factory and production wiring.

The factory is deliberately parameterised: every collaborator (settings,
pipeline, forwarder, audit sink) can be supplied externally. This is what
makes the proxy testable end-to-end without touching the network and
reusable as a library — Phase 5's pre-commit integration constructs a
``build_app`` variant with no forwarder at all.

Request lifecycle logic lives in :mod:`handler`; this file owns route
registration, lifespan management, and the production-defaults
constructor (:func:`create_default_app`).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.audit import (
    AuditSink,
    CompositeAuditSink,
    DuckdbAuditSink,
    InMemoryAuditSink,
    JsonlAuditSink,
    LoggingAuditSink,
    NullAuditSink,
    configure_logging,
)
from llm_guardrail_proxy.proxy.circuit_breaker import CircuitBreaker
from llm_guardrail_proxy.proxy.envelope import Provider
from llm_guardrail_proxy.proxy.forwarder import UpstreamForwarder
from llm_guardrail_proxy.proxy.handler import handle_proxied_request
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.middlewares import (
    PiiPolicy,
    PiiScanMiddleware,
    SecretScanMiddleware,
    TokenomicsMiddleware,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline
from llm_guardrail_proxy.proxy.providers import supported_paths
from llm_guardrail_proxy.proxy.scanning import PiiScanner, SecretScanner
from llm_guardrail_proxy.proxy.settings import ProxySettings
from llm_guardrail_proxy.proxy.stats import StatsRepository, build_stats_router

_LOGGER = logging.getLogger("llm_guardrail_proxy")


def build_app(
    *,
    settings: ProxySettings,
    pipeline: MiddlewarePipeline,
    forwarder: UpstreamForwarder,
    audit_sink: AuditSink | None = None,
    stats_repository: StatsRepository | None = None,
) -> FastAPI:
    """Construct a fully-wired FastAPI application.

    The lifespan hook does not own the ``httpx.AsyncClient``: that is the
    caller's responsibility, because in tests we hand in a pre-built client
    backed by :class:`httpx.MockTransport` whose lifecycle is managed by
    the test fixture.

    ``audit_sink`` defaults to a :class:`NullAuditSink` so callers that do
    not care about Phase 4 observability are not forced to construct one.
    ``stats_repository`` controls whether the read-only ``/stats/*``
    surface is mounted: when ``None``, the router is not added — callers
    that disable auditing or that build the app for the pre-commit hook
    have no reason to expose query endpoints.
    """

    sink: AuditSink = audit_sink if audit_sink is not None else NullAuditSink()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        _LOGGER.info(
            "llm-guardrail-proxy ready (paths=%s)", supported_paths()
        )
        try:
            yield
        finally:
            await sink.aclose()

    app = FastAPI(
        title="llm-guardrail-proxy",
        version="0.4.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.pipeline = pipeline
    app.state.forwarder = forwarder
    app.state.audit_sink = sink
    app.state.stats_repository = stats_repository

    if stats_repository is not None and settings.stats.enable_endpoint:
        app.include_router(
            build_stats_router(
                stats_repository,
                enable_dashboard=settings.stats.enable_dashboard,
            )
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def handle(request: Request) -> Response:
        return await handle_proxied_request(request)

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


# ----------------------------------------------------- production wiring


def _build_audit_sink(
    settings: ProxySettings,
) -> tuple[AuditSink, InMemoryAuditSink | None]:
    """Translate ``settings.audit`` into a concrete sink (or composite).

    Returns the user-facing :class:`AuditSink` (which may be a composite)
    *and* a direct handle to the in-memory ring that the stats endpoint
    consumes. The two are split so the route layer never has to inspect
    the structure of a composite to find the readable component — a
    coupling that would defeat the Protocol-based design.

    When auditing is disabled the ring handle is ``None``; the stats
    router is then not mounted by :func:`build_app`.
    """

    cfg = settings.audit
    if not cfg.enabled:
        return NullAuditSink(), None

    memory_sink = InMemoryAuditSink(capacity=cfg.memory_capacity)
    sinks: list[AuditSink] = [memory_sink]
    if cfg.log_enabled:
        sinks.append(LoggingAuditSink())
    if cfg.jsonl_path:
        sinks.append(JsonlAuditSink(cfg.jsonl_path))
    if cfg.duckdb_path:
        sinks.append(DuckdbAuditSink(cfg.duckdb_path))

    composite: AuditSink = sinks[0] if len(sinks) == 1 else CompositeAuditSink(sinks)
    return composite, memory_sink


def create_default_app() -> FastAPI:
    """Build an application with production defaults.

    Intended for ``uvicorn`` direct invocation. The ``httpx.AsyncClient`` is
    constructed here because, unlike the test fixture, the production path
    has no reason to share a client outside the app lifecycle.
    """

    settings = ProxySettings()

    # Logging is configured up-front so the LoggingAuditSink — instantiated
    # by ``_build_audit_sink`` — finds a properly initialised structlog
    # stack. Tests skip this call: they assert against the per-record
    # contract, not the on-the-wire log format.
    if settings.audit.log_enabled:
        configure_logging(
            json=settings.logging.format.lower() == "json",
            level=logging.getLevelName(settings.logging.level.upper()),
        )

    service = TokenomicsService(
        allow_unknown_models=settings.tokenomics.allow_unknown_models,
    )
    policy = ThresholdPolicy(
        max_tokens=settings.tokenomics.max_prompt_tokens,
        max_cost_usd=settings.tokenomics.max_prompt_cost_usd,
    )

    # Pipeline order is significant. Security-class verdicts (secret then
    # PII) run before the FinOps verdict so that a leaked credential or PII
    # value is surfaced even when the prompt would also have violated a
    # cost ceiling. PII follows secrets because a credential leak is
    # categorically worse: PII can sometimes be redacted in-place, but a
    # leaked API key cannot.
    middlewares: list[Middleware] = []
    if settings.scanning.enable_secrets:
        middlewares.append(SecretScanMiddleware(scanner=SecretScanner()))
    if settings.scanning.enable_pii:
        middlewares.append(
            PiiScanMiddleware(
                scanner=PiiScanner(
                    score_threshold=settings.scanning.pii_score_threshold,
                ),
                policy=PiiPolicy(settings.scanning.pii_policy),
            )
        )
    middlewares.append(TokenomicsMiddleware(service=service, policy=policy))
    pipeline = MiddlewarePipeline(middlewares)

    client = httpx.AsyncClient(timeout=settings.network.upstream_timeout_seconds)
    breaker = CircuitBreaker(
        failure_threshold=settings.breaker.failure_threshold,
        reset_seconds=settings.breaker.reset_seconds,
    )
    forwarder = UpstreamForwarder(
        client=client,
        breaker=breaker,
        origins={
            Provider.OPENAI: str(settings.network.openai_base_url),
            Provider.ANTHROPIC: str(settings.network.anthropic_base_url),
        },
    )

    audit_sink, memory_sink = _build_audit_sink(settings)
    return build_app(
        settings=settings,
        pipeline=pipeline,
        forwarder=forwarder,
        audit_sink=audit_sink,
        stats_repository=memory_sink,
    )
