"""FastAPI application factory.

The factory is deliberately parameterised: every collaborator (settings,
pipeline, forwarder, audit sink) can be supplied externally. This is what
makes the proxy testable end-to-end without touching the network and
reusable as a library — Phase 5's pre-commit integration constructs a
``build_app`` variant with no forwarder at all.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.audit import (
    AuditSink,
    CompositeAuditSink,
    DuckdbAuditSink,
    InMemoryAuditSink,
    JsonlAuditSink,
    LoggingAuditSink,
    NullAuditSink,
    build_audit_record,
    configure_logging,
)
from llm_guardrail_proxy.proxy.circuit_breaker import CircuitBreaker
from llm_guardrail_proxy.proxy.envelope import (
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
    PiiPolicy,
    PiiScanMiddleware,
    SecretScanMiddleware,
    TokenomicsMiddleware,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline, PipelineDecision
from llm_guardrail_proxy.proxy.providers import resolve_adapter, supported_paths
from llm_guardrail_proxy.proxy.scanning import PiiScanner, SecretScanner
from llm_guardrail_proxy.proxy.settings import ProxySettings

_LOGGER = logging.getLogger("llm_guardrail_proxy")

# Header used both to ingest a caller-supplied correlation ID and to echo
# the generated one back to the client. Lower-case form is what Starlette
# normalises to internally.
_REQUEST_ID_HEADER = "x-request-id"


def build_app(
    *,
    settings: ProxySettings,
    pipeline: MiddlewarePipeline,
    forwarder: UpstreamForwarder,
    audit_sink: AuditSink | None = None,
) -> FastAPI:
    """Construct a fully-wired FastAPI application.

    The lifespan hook does not own the ``httpx.AsyncClient``: that is the
    caller's responsibility, because in tests we hand in a pre-built client
    backed by :class:`httpx.MockTransport` whose lifecycle is managed by
    the test fixture.

    ``audit_sink`` defaults to a :class:`NullAuditSink` so callers that do
    not care about Phase 4 observability are not forced to construct one;
    :func:`create_default_app` wires the production sink stack.
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


# --------------------------------------------------------------- handler


def _resolve_request_id(request: Request) -> UUID:
    """Honour an inbound ``X-Request-Id`` when present, else generate one.

    Accepting a caller-supplied identifier lets distributed tracing
    correlate audit records with upstream logs the client already keeps.
    Malformed values are ignored silently — a bad header should not cause
    a 400 because the proxy can always generate a fresh ID.
    """

    raw = request.headers.get(_REQUEST_ID_HEADER)
    if raw:
        try:
            return UUID(raw)
        except ValueError:
            pass
    return uuid4()


async def _handle_proxied_request(request: Request) -> Response:
    """Inner request handler shared by every protected route.

    The handler is responsible for the full request lifecycle including
    audit emission. Audit is recorded on every terminating path —
    rejection, upstream failure, success — so the FinOps ledger can never
    miss an event. Exactly one record is produced per request.
    """

    pipeline: MiddlewarePipeline = request.app.state.pipeline
    forwarder: UpstreamForwarder = request.app.state.forwarder
    sink: AuditSink = request.app.state.audit_sink

    request_id = _resolve_request_id(request)
    started_at = time.perf_counter()

    raw_body = await request.body()

    try:
        adapter = resolve_adapter(request.url.path)
        parsed = adapter.parse(raw_body)
    except ProviderResolutionError as exc:
        # Pre-parse failures do not produce an audit record: there is no
        # provider, no model, no policy decision — nothing meaningful to
        # write. The 404 response carries the diagnostic.
        return JSONResponse(
            status_code=404,
            content={"error": "unknown_provider", "detail": str(exc)},
            headers={_REQUEST_ID_HEADER: str(request_id)},
        )
    except PromptExtractionError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "malformed_request", "detail": str(exc)},
            headers={_REQUEST_ID_HEADER: str(request_id)},
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
        latency_ms = (time.perf_counter() - started_at) * 1_000
        await _record_audit(
            sink,
            request_id=request_id,
            request=envelope,
            decision=decision,
            latency_ms=latency_ms,
            upstream_status_code=None,
            upstream_error=None,
        )
        return JSONResponse(
            status_code=decision.outcome.status_code,
            content={
                "error": decision.outcome.reason,
                "detail": decision.outcome.detail,
                "middleware": decision.rejecting_middleware,
                "annotations": decision.annotations,
                "request_id": str(request_id),
            },
            headers={_REQUEST_ID_HEADER: str(request_id)},
        )

    # ``final_request`` may differ from ``envelope`` if a middleware emitted
    # ``Mutate`` — the forwarder must ship the rewritten body, not the
    # original.
    try:
        upstream_response = await forwarder.forward(decision.final_request)
    except UpstreamError as exc:
        latency_ms = (time.perf_counter() - started_at) * 1_000
        await _record_audit(
            sink,
            request_id=request_id,
            request=envelope,
            decision=decision,
            latency_ms=latency_ms,
            upstream_status_code=None,
            upstream_error=str(exc),
        )
        return JSONResponse(
            status_code=502,
            content={"error": "upstream_unavailable", "detail": str(exc)},
            headers={_REQUEST_ID_HEADER: str(request_id)},
        )

    # Audit happens *before* the streaming body drains. Latency here is the
    # decision-plus-headers latency, not the full-body latency — that is
    # the figure FinOps actually cares about, and it is also the only one
    # the proxy can record without holding the response open.
    latency_ms = (time.perf_counter() - started_at) * 1_000
    await _record_audit(
        sink,
        request_id=request_id,
        request=envelope,
        decision=decision,
        latency_ms=latency_ms,
        upstream_status_code=upstream_response.status_code,
        upstream_error=None,
    )

    # Stamp the correlation header onto the response so clients can
    # cross-reference their own logs with the audit ledger.
    upstream_response.headers[_REQUEST_ID_HEADER] = str(request_id)
    return upstream_response


async def _record_audit(
    sink: AuditSink,
    *,
    request_id: UUID,
    request: ProxyRequest,
    decision: PipelineDecision,
    latency_ms: float,
    upstream_status_code: int | None,
    upstream_error: str | None,
) -> None:
    """Emit a single audit record, swallowing sink-side faults.

    A misbehaving sink (a full disk, a missing parent directory after the
    process started) must never propagate as a 5xx to the client: audit is
    secondary to traffic. Errors are logged at WARNING; Phase 4c's
    observability work can surface them through a metrics counter.
    """

    record = build_audit_record(
        request_id=request_id,
        request=request,
        decision=decision,
        latency_ms=latency_ms,
        upstream_status_code=upstream_status_code,
        upstream_error=upstream_error,
    )
    try:
        await sink.record(record)
    except Exception:  # pragma: no cover - defensive
        _LOGGER.warning(
            "audit sink rejected record request_id=%s", request_id, exc_info=True
        )


# ----------------------------------------------------- production wiring


def _build_audit_sink(settings: ProxySettings) -> AuditSink:
    """Translate ``settings`` into a concrete sink (or composite of sinks).

    The in-memory ring is *always* present when auditing is enabled: it
    backs the Phase 4c stats endpoint, and its cost is bounded by
    ``audit_memory_capacity``. Additional destinations (JSONL, DuckDB,
    structlog) are layered on top via :class:`CompositeAuditSink`, which
    isolates per-sink failures from the in-flight request.
    """

    if not settings.audit_enabled:
        return NullAuditSink()

    sinks: list[AuditSink] = [
        InMemoryAuditSink(capacity=settings.audit_memory_capacity),
    ]
    if settings.audit_log_enabled:
        sinks.append(LoggingAuditSink())
    if settings.audit_jsonl_path:
        sinks.append(JsonlAuditSink(settings.audit_jsonl_path))
    if settings.audit_duckdb_path:
        sinks.append(DuckdbAuditSink(settings.audit_duckdb_path))

    if len(sinks) == 1:
        return sinks[0]
    return CompositeAuditSink(sinks)


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
    if settings.audit_log_enabled:
        configure_logging(
            json=settings.log_format.lower() == "json",
            level=logging.getLevelName(settings.log_level.upper()),
        )
    service = TokenomicsService(allow_unknown_models=settings.allow_unknown_models)
    policy = ThresholdPolicy(
        max_tokens=settings.max_prompt_tokens,
        max_cost_usd=settings.max_prompt_cost_usd,
    )

    # Pipeline order is significant. Security-class verdicts (secret then
    # PII) run before the FinOps verdict so that a leaked credential or PII
    # value is surfaced even when the prompt would also have violated a
    # cost ceiling. PII follows secrets because a credential leak is
    # categorically worse: PII can sometimes be redacted in-place, but a
    # leaked API key cannot.
    middlewares: list[Middleware] = []
    if settings.enable_secret_scanning:
        middlewares.append(SecretScanMiddleware(scanner=SecretScanner()))
    if settings.enable_pii_scanning:
        middlewares.append(
            PiiScanMiddleware(
                scanner=PiiScanner(score_threshold=settings.pii_score_threshold),
                policy=PiiPolicy(settings.pii_policy),
            )
        )
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

    return build_app(
        settings=settings,
        pipeline=pipeline,
        forwarder=forwarder,
        audit_sink=_build_audit_sink(settings),
    )
