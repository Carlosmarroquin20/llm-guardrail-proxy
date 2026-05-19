"""FastAPI router for the read-only ``/stats`` surface.

The router is built from a :class:`StatsRepository` rather than from the
``InMemoryAuditSink`` directly so that the route layer never depends on
the concrete write-side implementation. This is the seam that lets Phase
4d swap in a DuckDB-backed repository without touching any HTTP code.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from llm_guardrail_proxy.proxy.audit import AuditRecord
from llm_guardrail_proxy.proxy.stats.repository import (
    StatsRepository,
    StatsSummary,
    summarise,
)


def build_stats_router(repository: StatsRepository) -> APIRouter:
    """Construct a router rooted at ``/stats``.

    ``include_in_schema=True`` is left as the FastAPI default so the
    endpoints appear in the OpenAPI document — operators routinely use
    that as the discovery surface.
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

    return router
