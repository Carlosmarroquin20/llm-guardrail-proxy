"""Stats endpoint — Phase 4c base.

Read-side counterpart to the audit plane. The package is deliberately
isolated from :mod:`llm_guardrail_proxy.proxy.audit`: audit owns the
write path and the record schema; stats owns aggregation and the HTTP
surface that exposes it. Audit never imports stats.

Public surface:

* :class:`StatsRepository` — Protocol every backing store must satisfy.
  The in-memory sink already does. A future DuckDB-backed repository
  (queries the persistent ledger) plugs in here without touching the
  router.
* :func:`summarise` — pure aggregator returning a :class:`StatsSummary`.
* :class:`StatsSummary` — frozen response model.
* :func:`build_stats_router` — FastAPI router factory.
"""

from llm_guardrail_proxy.proxy.stats.repository import (
    StatsRepository,
    StatsSummary,
    summarise,
)
from llm_guardrail_proxy.proxy.stats.router import build_stats_router

__all__ = [
    "StatsRepository",
    "StatsSummary",
    "build_stats_router",
    "summarise",
]
