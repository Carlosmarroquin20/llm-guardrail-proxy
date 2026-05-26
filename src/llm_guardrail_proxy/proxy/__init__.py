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

Public surface
==============

This module re-exports only the entry points needed to *run* the proxy
(``build_app``, ``create_default_app``, ``ProxySettings``) and the
extension surface needed to *write a custom middleware* (the envelope
types, the ``Middleware`` Protocol, and ``MiddlewarePipeline``).

Internal machinery — audit sinks, stats aggregators, parsed prompt
values, finding records — is *not* re-exported. Importing those from
their owning sub-module (``proxy.audit``, ``proxy.stats``,
``proxy.envelope``) is the supported path; the narrower surface here
keeps it obvious which names belong to the public API.
"""

from llm_guardrail_proxy.proxy.app import build_app, create_default_app
from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    Mutate,
    Provider,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline
from llm_guardrail_proxy.proxy.settings import ProxySettings

__all__ = [
    # Factories
    "build_app",
    "create_default_app",
    # Configuration
    "ProxySettings",
    # Pipeline contract
    "Middleware",
    "MiddlewarePipeline",
    # Middleware-authoring surface
    "Continue",
    "MiddlewareOutcome",
    "Mutate",
    "Provider",
    "ProxyRequest",
    "Reject",
]
