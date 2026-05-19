"""Async proxy surface — Phase 2.

The proxy layer is structured around three orthogonal seams:

* **Provider adapters** (``providers``) — normalise heterogeneous LLM request
  bodies into a single parsed envelope.
* **Middleware pipeline** (``pipeline`` + ``middleware``) — a typed,
  ordered chain that runs against the envelope and may short-circuit with a
  structured rejection.
* **Forwarder** (``forwarder``) — streams approved requests upstream behind
  a circuit breaker.

Phase 3+ extensions (PII, secret scanning, audit) plug in as additional
middleware without touching the orchestrator.
"""

from llm_guardrail_proxy.proxy.app import build_app, create_default_app
from llm_guardrail_proxy.proxy.audit import (
    AuditRecord,
    AuditSink,
    CompositeAuditSink,
    DuckdbAuditSink,
    EnforcementVerdict,
    InMemoryAuditSink,
    JsonlAuditSink,
    LoggingAuditSink,
    NullAuditSink,
    configure_logging,
)
from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    Mutate,
    ParsedPrompt,
    Provider,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline
from llm_guardrail_proxy.proxy.settings import ProxySettings

__all__ = [
    "AuditRecord",
    "AuditSink",
    "CompositeAuditSink",
    "Continue",
    "DuckdbAuditSink",
    "EnforcementVerdict",
    "InMemoryAuditSink",
    "JsonlAuditSink",
    "LoggingAuditSink",
    "Middleware",
    "MiddlewareOutcome",
    "MiddlewarePipeline",
    "Mutate",
    "NullAuditSink",
    "ParsedPrompt",
    "Provider",
    "ProxyRequest",
    "ProxySettings",
    "Reject",
    "build_app",
    "configure_logging",
    "create_default_app",
]
