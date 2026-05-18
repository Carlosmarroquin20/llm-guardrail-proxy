"""Audit plane — Phase 4 base.

The audit layer captures a structured, post-hoc summary of every proxied
request. Three properties matter:

* **Decoupled from middleware.** Audit lives in the route handler because
  it needs information that spans the full request lifecycle — pipeline
  verdict, upstream status, latency — none of which any single middleware
  sees in isolation.
* **No re-leakage.** Records carry counts and pre-redacted previews, never
  raw prompt content or full secret/PII values. The audit plane must not
  become an exfiltration channel.
* **Pluggable sink.** ``AuditSink`` is a Protocol; Phase 4b will register
  a DuckDB-backed implementation, and Phase 4c (the read-only stats
  endpoint) will query whichever sink the deployment configured.
"""

from llm_guardrail_proxy.proxy.audit.records import (
    AuditRecord,
    EnforcementVerdict,
    FindingRecord,
    build_audit_record,
    findings_from_pipeline,
)
from llm_guardrail_proxy.proxy.audit.sinks import (
    AuditSink,
    InMemoryAuditSink,
    JsonlAuditSink,
    NullAuditSink,
)

__all__ = [
    "AuditRecord",
    "AuditSink",
    "EnforcementVerdict",
    "FindingRecord",
    "InMemoryAuditSink",
    "JsonlAuditSink",
    "NullAuditSink",
    "build_audit_record",
    "findings_from_pipeline",
]
