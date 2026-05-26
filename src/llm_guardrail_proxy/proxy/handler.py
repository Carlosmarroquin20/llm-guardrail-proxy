"""Request handler for the proxy's protected routes.

Lives in its own module because :mod:`app` had grown three concerns
(application factory, request lifecycle, production wiring) under one
file. Splitting them keeps each evolution path independent: the handler
changes when the middleware contract or audit shape changes; the
factory changes when route registration patterns change; the
production wiring rarely changes at all.

The handler is also the single place where audit recording happens.
That was a deliberate Phase 4 decision (it needs the upstream status
code and end-to-end latency that no individual middleware sees);
keeping it here documents that constraint structurally as well as in
prose.
"""

from __future__ import annotations

import logging
import time
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from llm_guardrail_proxy.proxy.audit import AuditSink, build_audit_record
from llm_guardrail_proxy.proxy.envelope import ProxyRequest, Reject
from llm_guardrail_proxy.proxy.exceptions import (
    PromptExtractionError,
    ProviderResolutionError,
    UpstreamError,
)
from llm_guardrail_proxy.proxy.forwarder import UpstreamForwarder
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline, PipelineDecision
from llm_guardrail_proxy.proxy.providers import resolve_adapter

_LOGGER = logging.getLogger("llm_guardrail_proxy")

# Header used both to ingest a caller-supplied correlation ID and to echo
# the generated one back to the client. Lower-case form is what Starlette
# normalises to internally.
REQUEST_ID_HEADER = "x-request-id"


def resolve_request_id(request: Request) -> UUID:
    """Honour an inbound ``X-Request-Id`` when present, else generate one.

    Accepting a caller-supplied identifier lets distributed tracing
    correlate audit records with upstream logs the client already keeps.
    Malformed values are ignored silently — a bad header should not cause
    a 400 because the proxy can always generate a fresh ID.
    """

    raw = request.headers.get(REQUEST_ID_HEADER)
    if raw:
        try:
            return UUID(raw)
        except ValueError:
            pass
    return uuid4()


async def handle_proxied_request(request: Request) -> Response:
    """Inner request handler shared by every protected route.

    The handler is responsible for the full request lifecycle including
    audit emission. Audit is recorded on every terminating path —
    rejection, upstream failure, success — so the FinOps ledger can never
    miss an event. Exactly one record is produced per request.
    """

    pipeline: MiddlewarePipeline = request.app.state.pipeline
    forwarder: UpstreamForwarder = request.app.state.forwarder
    sink: AuditSink = request.app.state.audit_sink

    request_id = resolve_request_id(request)
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
            headers={REQUEST_ID_HEADER: str(request_id)},
        )
    except PromptExtractionError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "malformed_request", "detail": str(exc)},
            headers={REQUEST_ID_HEADER: str(request_id)},
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
            headers={REQUEST_ID_HEADER: str(request_id)},
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
            headers={REQUEST_ID_HEADER: str(request_id)},
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
    upstream_response.headers[REQUEST_ID_HEADER] = str(request_id)
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
