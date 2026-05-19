"""Tests for the composite (fan-out) audit sink."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from llm_guardrail_proxy.proxy.audit import (
    AuditRecord,
    CompositeAuditSink,
    EnforcementVerdict,
)
from llm_guardrail_proxy.proxy.envelope import Provider


def _record() -> AuditRecord:
    return AuditRecord(
        provider=Provider.OPENAI,
        path="/v1/chat/completions",
        model="gpt-4o",
        verdict=EnforcementVerdict.ALLOWED,
        latency_ms=1.0,
    )


@dataclass
class _RecordingSink:
    """Stub sink that appends every record to a shared list."""

    seen: list[AuditRecord] = field(default_factory=list)
    closed: bool = False

    async def record(self, entry: AuditRecord) -> None:
        self.seen.append(entry)

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class _ExplodingSink:
    """Stub sink that always raises on record / close — guards the fault
    isolation contract of the composite."""

    on_record_called: int = 0
    on_close_called: int = 0

    async def record(self, entry: AuditRecord) -> None:
        self.on_record_called += 1
        raise RuntimeError("boom")

    async def aclose(self) -> None:
        self.on_close_called += 1
        raise RuntimeError("boom-close")


class TestCompositeBasics:
    async def test_record_fans_out_to_every_member(self) -> None:
        a, b = _RecordingSink(), _RecordingSink()
        composite = CompositeAuditSink([a, b])
        entry = _record()
        await composite.record(entry)
        assert a.seen == [entry]
        assert b.seen == [entry]

    async def test_aclose_propagates_to_every_member(self) -> None:
        a, b = _RecordingSink(), _RecordingSink()
        composite = CompositeAuditSink([a, b])
        await composite.aclose()
        assert a.closed and b.closed

    def test_empty_sink_list_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            CompositeAuditSink([])


class TestFaultIsolation:
    async def test_failing_sink_does_not_block_subsequent_sinks(self) -> None:
        good = _RecordingSink()
        bad = _ExplodingSink()
        # Order matters: ``bad`` runs *before* ``good`` so any naive
        # implementation that re-raises would skip ``good``.
        composite = CompositeAuditSink([bad, good])
        entry = _record()
        await composite.record(entry)
        assert bad.on_record_called == 1
        assert good.seen == [entry], (
            "failure in the first sink must not skip downstream sinks"
        )

    async def test_failing_aclose_does_not_block_subsequent_sinks(self) -> None:
        good = _RecordingSink()
        bad = _ExplodingSink()
        composite = CompositeAuditSink([bad, good])
        await composite.aclose()
        assert good.closed
        assert bad.on_close_called == 1
