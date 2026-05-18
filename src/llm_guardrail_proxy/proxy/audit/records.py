"""Audit record schema.

A single :class:`AuditRecord` captures everything the FinOps and security
layers need to know about a proxied request *after the fact*. The schema
deliberately omits prompt content: scanners have already produced
redacted previews of any sensitive value, and including the original
text would let the audit log itself become an exfiltration channel.

Decimal is preserved as a typed field so persistence layers downstream
(DuckDB in Phase 4b) can keep monetary precision; the model serialises
Decimal as a JSON string to avoid float drift through the JSONL sink.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from llm_guardrail_proxy.proxy.envelope import Provider, ProxyRequest
from llm_guardrail_proxy.proxy.pipeline import PipelineDecision


class EnforcementVerdict(str, Enum):
    """Top-level outcome attached to every audit record."""

    ALLOWED = "allowed"
    REJECTED = "rejected"


class FindingRecord(BaseModel):
    """Audit-safe projection of a single scanner finding.

    ``span`` is intentionally absent — span offsets are tied to the
    pre-redaction text and become misleading after a ``Mutate`` outcome
    rewrites the body. ``preview`` already carries enough triage signal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scanner: str
    kind: str
    label: str
    severity: str
    preview: str


class AuditRecord(BaseModel):
    """Immutable snapshot of one proxied request.

    The record is built at exactly one site (:func:`build_audit_record`)
    and stored verbatim by every sink. Mutation after construction is
    forbidden so that two sinks observing the same request can never
    disagree on its contents.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    provider: Provider
    path: str
    model: str

    verdict: EnforcementVerdict
    rejecting_middleware: str | None = None
    reject_reason: str | None = None
    reject_status_code: int | None = None

    token_count: int | None = None
    estimated_cost_usd: Decimal | None = None

    mutations_applied: bool = False
    findings: tuple[FindingRecord, ...] = ()

    latency_ms: float
    upstream_status_code: int | None = None
    upstream_error: str | None = None


# --------------------------------------------------------------- helpers


def findings_from_pipeline(
    annotations: Mapping[str, Mapping[str, Any]],
) -> tuple[FindingRecord, ...]:
    """Flatten per-middleware annotations into a single findings tuple.

    The pipeline aggregates annotations as ``{middleware_name: {...}}``;
    every scanner middleware emits a ``findings`` list under that key.
    Walking it here keeps the audit layer ignorant of which specific
    middlewares produce findings — Phase 4c can add a new scanner and
    the audit ledger picks it up without code changes.
    """

    out: list[FindingRecord] = []
    for scanner, payload in annotations.items():
        findings = payload.get("findings")
        if not isinstance(findings, list):
            continue
        for raw in findings:
            if not isinstance(raw, dict):
                continue
            try:
                out.append(
                    FindingRecord(
                        scanner=scanner,
                        kind=str(raw["kind"]),
                        label=str(raw["label"]),
                        severity=str(raw["severity"]),
                        preview=str(raw["preview"]),
                    )
                )
            except KeyError:
                # Defensive: middleware authored outside this repo may emit
                # partial findings. Skip rather than break audit recording.
                continue
    return tuple(out)


def _tokenomics_annotation(
    annotations: Mapping[str, Mapping[str, Any]],
) -> tuple[int | None, Decimal | None]:
    """Extract token/cost figures from the tokenomics middleware annotations.

    Falls back to ``(None, None)`` when the middleware did not run or did
    not annotate — a permissive contract so audit recording never fails
    because a deployment opted to disable tokenomics.
    """

    payload = annotations.get("tokenomics")
    if not isinstance(payload, Mapping):
        return None, None

    token_count = payload.get("token_count")
    cost_raw = payload.get("estimated_cost_usd")

    cost: Decimal | None = None
    if isinstance(cost_raw, (str, int)):
        try:
            cost = Decimal(cost_raw)
        except Exception:  # pragma: no cover - defensive only
            cost = None
    elif isinstance(cost_raw, Decimal):
        cost = cost_raw

    return (
        token_count if isinstance(token_count, int) else None,
        cost,
    )


def build_audit_record(
    *,
    request_id: UUID,
    request: ProxyRequest,
    decision: PipelineDecision,
    latency_ms: float,
    upstream_status_code: int | None,
    upstream_error: str | None,
) -> AuditRecord:
    """Construct an :class:`AuditRecord` from the in-flight request state.

    The single construction site keeps the schema/transport coupling in
    one place; any future field additions need only be threaded through
    this signature.
    """

    rejected = decision.rejecting_middleware is not None
    reject = decision.outcome if rejected else None
    reject_reason = getattr(reject, "reason", None)
    reject_status = getattr(reject, "status_code", None)

    mutations_applied = decision.final_request is not request
    token_count, cost = _tokenomics_annotation(decision.annotations)

    return AuditRecord(
        request_id=request_id,
        provider=request.parsed.provider,
        path=request.path,
        model=request.parsed.model,
        verdict=EnforcementVerdict.REJECTED if rejected else EnforcementVerdict.ALLOWED,
        rejecting_middleware=decision.rejecting_middleware,
        reject_reason=reject_reason,
        reject_status_code=reject_status,
        token_count=token_count,
        estimated_cost_usd=cost,
        mutations_applied=mutations_applied,
        findings=findings_from_pipeline(decision.annotations),
        latency_ms=latency_ms,
        upstream_status_code=upstream_status_code,
        upstream_error=upstream_error,
    )


# Re-exported so callers can build ad-hoc records without importing typing
# internals. Kept at module bottom because it is purely a convenience alias.
def _iter_records(records: Iterable[AuditRecord]) -> tuple[AuditRecord, ...]:  # noqa: D401
    return tuple(records)
