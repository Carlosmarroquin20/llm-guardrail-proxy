"""Unit tests for the audit record schema and builders."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from llm_guardrail_proxy.proxy.audit import (
    AuditRecord,
    EnforcementVerdict,
    FindingRecord,
    build_audit_record,
    findings_from_pipeline,
)
from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    ParsedPrompt,
    Provider,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.pipeline import PipelineDecision


def _envelope(model: str = "gpt-4o", content: str = "hi") -> ProxyRequest:
    return ProxyRequest(
        path="/v1/chat/completions",
        method="POST",
        headers={},
        raw_body=b"{}",
        parsed=ParsedPrompt(provider=Provider.OPENAI, model=model, content=content),
    )


# ---------------------------------------------------------------- schema


class TestAuditRecordSchema:
    def test_minimal_record_can_be_constructed(self) -> None:
        rec = AuditRecord(
            provider=Provider.OPENAI,
            path="/v1/chat/completions",
            model="gpt-4o",
            verdict=EnforcementVerdict.ALLOWED,
            latency_ms=12.3,
        )
        assert isinstance(rec.request_id, UUID)
        assert rec.timestamp.tzinfo is timezone.utc

    def test_record_is_frozen(self) -> None:
        rec = AuditRecord(
            provider=Provider.OPENAI,
            path="/v1/chat/completions",
            model="gpt-4o",
            verdict=EnforcementVerdict.ALLOWED,
            latency_ms=1.0,
        )
        with pytest.raises(ValidationError):
            rec.latency_ms = 9.9  # type: ignore[misc]

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuditRecord(
                provider=Provider.OPENAI,
                path="/v1/chat/completions",
                model="gpt-4o",
                verdict=EnforcementVerdict.ALLOWED,
                latency_ms=1.0,
                unexpected="value",  # type: ignore[call-arg]
            )

    def test_decimal_field_serialises_as_string(self) -> None:
        rec = AuditRecord(
            provider=Provider.OPENAI,
            path="/v1/chat/completions",
            model="gpt-4o",
            verdict=EnforcementVerdict.ALLOWED,
            latency_ms=1.0,
            estimated_cost_usd=Decimal("0.001234"),
        )
        as_json = json.loads(rec.model_dump_json())
        # Pydantic v2 emits Decimal as a JSON string by default — a critical
        # property because the JSONL sink must not round-trip via float.
        assert as_json["estimated_cost_usd"] == "0.001234"


# --------------------------------------------------- findings projection


class TestFindingsFromPipeline:
    def test_collects_findings_across_middlewares(self) -> None:
        annotations = {
            "secret_scan": {
                "finding_count": 1,
                "findings": [
                    {
                        "kind": "aws_access_key_id",
                        "label": "AWS Access Key ID",
                        "severity": "high",
                        "span": [0, 20],
                        "preview": "AKIA***OP",
                    }
                ],
            },
            "pii_scan": {
                "finding_count": 1,
                "findings": [
                    {
                        "kind": "pii_email_address",
                        "label": "EMAIL_ADDRESS",
                        "severity": "medium",
                        "span": [30, 40],
                        "preview": "carl***om",
                    }
                ],
            },
            "tokenomics": {"token_count": 10, "estimated_cost_usd": "0.001"},
        }
        findings = findings_from_pipeline(annotations)
        scanners = {f.scanner for f in findings}
        assert scanners == {"secret_scan", "pii_scan"}
        for f in findings:
            assert isinstance(f, FindingRecord)

    def test_partial_finding_is_skipped(self) -> None:
        annotations = {
            "secret_scan": {
                "findings": [
                    {"kind": "x", "label": "X"},  # missing severity/preview
                    {
                        "kind": "y",
                        "label": "Y",
                        "severity": "high",
                        "preview": "abc***",
                    },
                ]
            }
        }
        findings = findings_from_pipeline(annotations)
        # Only the well-formed finding survives.
        assert len(findings) == 1
        assert findings[0].kind == "y"

    def test_absent_findings_key_is_tolerated(self) -> None:
        # Tokenomics never emits a ``findings`` list. The projector must
        # walk past it cleanly rather than raise.
        annotations = {"tokenomics": {"token_count": 7}}
        assert findings_from_pipeline(annotations) == ()


# ----------------------------------------------------- record builder


class TestBuildAuditRecord:
    def test_allowed_request_is_recorded(self) -> None:
        env = _envelope()
        decision = PipelineDecision(
            outcome=Continue(),
            rejecting_middleware=None,
            final_request=env,
            annotations={
                "tokenomics": {
                    "token_count": 5,
                    "estimated_cost_usd": "0.0001",
                }
            },
        )
        rid = uuid4()
        rec = build_audit_record(
            request_id=rid,
            request=env,
            decision=decision,
            latency_ms=4.5,
            upstream_status_code=200,
            upstream_error=None,
        )
        assert rec.request_id == rid
        assert rec.verdict is EnforcementVerdict.ALLOWED
        assert rec.token_count == 5
        assert rec.estimated_cost_usd == Decimal("0.0001")
        assert rec.upstream_status_code == 200
        assert rec.mutations_applied is False

    def test_rejected_request_carries_middleware_metadata(self) -> None:
        env = _envelope()
        decision = PipelineDecision(
            outcome=Reject(
                status_code=413,
                reason="tokenomics_policy_violation",
                detail="too big",
            ),
            rejecting_middleware="tokenomics",
            final_request=env,
            annotations={
                "tokenomics": {"token_count": 9999, "estimated_cost_usd": "1.0"}
            },
        )
        rec = build_audit_record(
            request_id=uuid4(),
            request=env,
            decision=decision,
            latency_ms=2.0,
            upstream_status_code=None,
            upstream_error=None,
        )
        assert rec.verdict is EnforcementVerdict.REJECTED
        assert rec.rejecting_middleware == "tokenomics"
        assert rec.reject_reason == "tokenomics_policy_violation"
        assert rec.reject_status_code == 413
        assert rec.upstream_status_code is None

    def test_mutation_is_reflected(self) -> None:
        original = _envelope()
        rewritten = _envelope(content="redacted")
        decision = PipelineDecision(
            outcome=Continue(),
            rejecting_middleware=None,
            final_request=rewritten,
            annotations={},
        )
        rec = build_audit_record(
            request_id=uuid4(),
            request=original,
            decision=decision,
            latency_ms=1.0,
            upstream_status_code=200,
            upstream_error=None,
        )
        assert rec.mutations_applied is True

    def test_upstream_error_is_captured(self) -> None:
        env = _envelope()
        decision = PipelineDecision(
            outcome=Continue(),
            rejecting_middleware=None,
            final_request=env,
            annotations={},
        )
        rec = build_audit_record(
            request_id=uuid4(),
            request=env,
            decision=decision,
            latency_ms=8.0,
            upstream_status_code=None,
            upstream_error="connection refused",
        )
        # Verdict remains ALLOWED — the pipeline did allow the request; it
        # was the upstream call that failed. Phase 4's stats endpoint will
        # surface ``upstream_error`` as a distinct counter.
        assert rec.verdict is EnforcementVerdict.ALLOWED
        assert rec.upstream_error == "connection refused"


def test_timestamp_is_in_utc() -> None:
    rec = AuditRecord(
        provider=Provider.OPENAI,
        path="/v1/chat/completions",
        model="gpt-4o",
        verdict=EnforcementVerdict.ALLOWED,
        latency_ms=1.0,
    )
    # The default factory uses ``datetime.now(timezone.utc)``; ensure the
    # contract is honoured so downstream JSONL never carries a naive value.
    assert rec.timestamp.tzinfo is not None
    assert rec.timestamp <= datetime.now(timezone.utc)
