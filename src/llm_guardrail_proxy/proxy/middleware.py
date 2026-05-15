"""Middleware contract.

A ``Protocol`` (rather than an abstract base class) is used so middlewares
can be implemented as plain dataclasses, functions wrapped in adapters, or
third-party objects, with structural typing as the only requirement. This
matters for Phase 5, where the CLI must be able to load middlewares from
arbitrary import paths without inheriting a project-internal base.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from llm_guardrail_proxy.proxy.envelope import MiddlewareOutcome, ProxyRequest


@runtime_checkable
class Middleware(Protocol):
    """Asynchronous, single-method strategy executed against a ``ProxyRequest``.

    The ``name`` attribute exists for observability: audit records and
    rejection responses cite the middleware that produced the verdict, and
    relying on ``type(...).__name__`` would couple the public identifier to
    Python class names that may legitimately be renamed during refactors.
    """

    name: str

    async def process(self, request: ProxyRequest) -> MiddlewareOutcome:
        """Inspect ``request`` and return a continue/reject verdict.

        Implementations must be side-effect-free with respect to the
        envelope: it is frozen, but middlewares should also avoid touching
        global state so the pipeline remains deterministic under
        concurrency. State-bearing middlewares (rate limiters, audit
        sinks) should keep their state internal and threadsafe.
        """
        ...
