"""Proxy-layer exception hierarchy.

These complement (but never replace) the core ``GuardrailError`` family.
The split exists because proxy faults are transport-level concerns — they
must map cleanly onto HTTP status codes at the route boundary, whereas
core faults are policy-level and may surface in non-HTTP contexts
(CLI, pre-commit) without modification.
"""

from __future__ import annotations


class ProxyError(Exception):
    """Base class for all proxy-layer faults."""


class ProviderResolutionError(ProxyError):
    """Raised when an incoming request cannot be mapped to a known provider.

    The proxy refuses to forward an opaque payload it cannot inspect:
    a request the guardrails cannot read is a request the guardrails cannot
    govern, and silent passthrough would defeat the entire purpose of the
    proxy.
    """


class PromptExtractionError(ProxyError):
    """Raised when a recognised provider payload is malformed.

    Distinct from ``ProviderResolutionError`` so that monitoring can
    distinguish "unknown shape" from "known shape, broken body".
    """


class UpstreamError(ProxyError):
    """Raised when the upstream LLM provider is unreachable or misbehaves.

    Wraps the underlying ``httpx`` exception via ``__cause__`` chaining so
    Phase 4 observability can attribute failures to network vs. protocol
    vs. timeout categories without re-parsing this layer's message text.
    """


class CircuitOpenError(UpstreamError):
    """Raised when the breaker is currently open and the request is rejected.

    Subclasses ``UpstreamError`` so a single ``except UpstreamError`` block
    at the route boundary covers both "tried and failed" and "refused to try".
    """
