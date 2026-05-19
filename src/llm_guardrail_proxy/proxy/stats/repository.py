"""Aggregator + read-side Protocol for the stats endpoint.

The aggregator is a pure function: given an iterable of
:class:`AuditRecord`, it returns a :class:`StatsSummary`. Keeping it free
of I/O — no FastAPI types, no async — makes it the single place where
FinOps semantics live, and lets the test suite cover edge cases without
spinning up the ASGI stack.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Iterable, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from llm_guardrail_proxy.proxy.audit import AuditRecord, EnforcementVerdict


@runtime_checkable
class StatsRepository(Protocol):
    """Read-side contract for the stats router.

    A repository is any object that can produce the current set of audit
    records on demand. :class:`InMemoryAuditSink` satisfies the contract
    via its ``records`` property; Phase 4d may add a DuckDB-backed
    implementation that issues SELECTs against the persistent ledger.
    """

    @property
    def records(self) -> Sequence[AuditRecord]: ...


class StatsSummary(BaseModel):
    """Aggregated view of the records currently held by the repository.

    Sort orders are stable and documented per field so a client polling
    the endpoint sees deterministic output.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_requests: int = Field(ge=0)
    allowed: int = Field(ge=0)
    rejected: int = Field(ge=0)
    rejection_rate: float = Field(ge=0.0, le=1.0)

    total_estimated_cost_usd: Decimal = Field(default=Decimal("0"))
    avg_latency_ms: float = Field(ge=0.0)

    # Counters are emitted as plain dicts because the JSON consumer
    # (a dashboard, a curl pipeline into jq) is far easier to write
    # against a flat mapping than against a list of tuples.
    rejections_by_middleware: dict[str, int] = Field(default_factory=dict)
    requests_by_model: dict[str, int] = Field(default_factory=dict)
    findings_by_scanner: dict[str, int] = Field(default_factory=dict)


def summarise(records: Iterable[AuditRecord]) -> StatsSummary:
    """Project a record sequence onto a :class:`StatsSummary`.

    The function is total: an empty iterable produces a zeroed summary
    (rather than raising), which matches the expectation that a freshly
    started proxy with no traffic still serves a well-formed response.
    """

    materialised: list[AuditRecord] = list(records)
    total = len(materialised)

    if total == 0:
        return StatsSummary(
            total_requests=0,
            allowed=0,
            rejected=0,
            rejection_rate=0.0,
            avg_latency_ms=0.0,
        )

    allowed = sum(
        1 for r in materialised if r.verdict is EnforcementVerdict.ALLOWED
    )
    rejected = total - allowed

    # Decimal accumulation stays exact; converting to float for the JSON
    # payload would defeat the no-float-drift contract that motivated the
    # whole pricing module.
    cost = sum(
        (r.estimated_cost_usd or Decimal("0") for r in materialised),
        Decimal("0"),
    )

    latency_sum = sum(r.latency_ms for r in materialised)
    avg_latency = latency_sum / total

    rejections = Counter(
        r.rejecting_middleware
        for r in materialised
        if r.rejecting_middleware is not None
    )
    models = Counter(r.model for r in materialised)
    scanners: Counter[str] = Counter()
    for r in materialised:
        for finding in r.findings:
            scanners[finding.scanner] += 1

    return StatsSummary(
        total_requests=total,
        allowed=allowed,
        rejected=rejected,
        rejection_rate=rejected / total,
        total_estimated_cost_usd=cost,
        avg_latency_ms=avg_latency,
        rejections_by_middleware=dict(rejections.most_common()),
        requests_by_model=dict(models.most_common()),
        findings_by_scanner=dict(scanners.most_common()),
    )
