"""FastAPI router for the read-only ``/stats`` surface.

The router is built from a :class:`StatsRepository` rather than from the
``InMemoryAuditSink`` directly so that the route layer never depends on
the concrete write-side implementation. This is the seam that lets Phase
4d swap in a DuckDB-backed repository without touching any HTTP code.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from llm_guardrail_proxy.proxy.audit import AuditRecord
from llm_guardrail_proxy.proxy.stats.dashboard import DASHBOARD_HTML
from llm_guardrail_proxy.proxy.stats.repository import (
    StatsRepository,
    StatsSummary,
    summarise,
)


def build_stats_router(
    repository: StatsRepository,
    *,
    enable_dashboard: bool = True,
) -> APIRouter:
    """Construct a router rooted at ``/stats``.

    ``include_in_schema=True`` is left as the FastAPI default so the
    endpoints appear in the OpenAPI document — operators routinely use
    that as the discovery surface.

    ``enable_dashboard`` controls whether the HTML dashboard is mounted.
    The dashboard reads from the same JSON endpoints, so disabling it
    does not affect the API surface other consumers depend on.
    """

    router = APIRouter(prefix="/stats", tags=["stats"])

    @router.get(
        "/summary",
        response_model=StatsSummary,
        summary="Aggregated view of recently audited requests.",
    )
    async def summary() -> StatsSummary:
        # ``summarise`` is a pure function; no awaitable work to do here,
        # but the route is async so FastAPI runs it on the event loop
        # instead of in the threadpool — cheaper for this CPU-bound work.
        return summarise(repository.records)

    @router.get(
        "/recent",
        response_model=list[AuditRecord],
        summary="Most recent audited requests, newest first.",
    )
    async def recent(
        limit: Annotated[int, Query(ge=1, le=1_000)] = 50,
    ) -> list[AuditRecord]:
        # The repository stores records in arrival order; reverse so the
        # newest is first, then truncate. This is the order a dashboard
        # wants by default.
        snapshot = list(repository.records)
        snapshot.reverse()
        return snapshot[:limit]

    if enable_dashboard:
        @router.get(
            "/dashboard",
            response_class=HTMLResponse,
            include_in_schema=False,
            summary="Auto-refreshing HTML view of the audit ring.",
        )
        async def dashboard() -> HTMLResponse:
            # The HTML is a static constant; serving it from the same
            # router keeps the dashboard discoverable next to the JSON
            # endpoints it consumes. ``include_in_schema=False`` keeps
            # the OpenAPI document focused on the machine surface.
            return HTMLResponse(DASHBOARD_HTML)

    return router
