"""Composite audit sink — fan-out with isolated failure semantics.

When more than one sink is configured (in-memory ring + JSONL + DuckDB,
say), they must all observe the same record stream, and a transient
fault in one (a full disk, a locked DuckDB file) must never starve the
others. The composite enforces both properties: it dispatches every
record to every member sink in declaration order, logging — but not
raising — any per-sink exception.

``aclose`` follows the same isolation contract: a failure on one sink
must not prevent the others from flushing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from llm_guardrail_proxy.proxy.audit.records import AuditRecord
from llm_guardrail_proxy.proxy.audit.sinks import AuditSink

_LOGGER = logging.getLogger("llm_guardrail_proxy.audit")


class CompositeAuditSink:
    """Fan a single record out to multiple downstream sinks.

    Parameters
    ----------
    sinks:
        Ordered iterable of :class:`AuditSink` instances. The composite
        materialises a tuple at construction time so callers cannot mutate
        the fan-out after the fact.
    """

    __slots__ = ("_sinks",)

    def __init__(self, sinks: Iterable[AuditSink]) -> None:
        materialised: tuple[AuditSink, ...] = tuple(sinks)
        if not materialised:
            raise ValueError(
                "CompositeAuditSink requires at least one underlying sink."
            )
        self._sinks = materialised

    @property
    def sinks(self) -> Sequence[AuditSink]:
        return self._sinks

    async def record(self, entry: AuditRecord) -> None:
        for sink in self._sinks:
            try:
                await sink.record(entry)
            except Exception:  # pragma: no cover - defensive
                # Audit is secondary to traffic. Surfacing the exception
                # would cascade into a 5xx for the client; logging keeps
                # the event diagnosable without that blast radius.
                _LOGGER.warning(
                    "audit sink %s failed to record entry",
                    type(sink).__name__,
                    exc_info=True,
                )

    async def aclose(self) -> None:
        for sink in self._sinks:
            try:
                await sink.aclose()
            except Exception:  # pragma: no cover - defensive
                _LOGGER.warning(
                    "audit sink %s raised during aclose",
                    type(sink).__name__,
                    exc_info=True,
                )
