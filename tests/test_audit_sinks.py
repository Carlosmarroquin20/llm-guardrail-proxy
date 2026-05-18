"""Tests for the audit sink implementations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_guardrail_proxy.proxy.audit import (
    AuditRecord,
    EnforcementVerdict,
    InMemoryAuditSink,
    JsonlAuditSink,
    NullAuditSink,
)
from llm_guardrail_proxy.proxy.envelope import Provider


def _record(model: str = "gpt-4o") -> AuditRecord:
    return AuditRecord(
        provider=Provider.OPENAI,
        path="/v1/chat/completions",
        model=model,
        verdict=EnforcementVerdict.ALLOWED,
        latency_ms=1.0,
    )


# ----------------------------------------------------------------- null


class TestNullAuditSink:
    async def test_record_is_a_noop(self) -> None:
        sink = NullAuditSink()
        await sink.record(_record())
        await sink.aclose()
        # No assertion beyond "does not raise" — the contract of NullSink
        # is that it discards every input silently.


# -------------------------------------------------------------- in-memory


class TestInMemoryAuditSink:
    async def test_records_are_retained(self) -> None:
        sink = InMemoryAuditSink(capacity=3)
        for i in range(3):
            await sink.record(_record(model=f"m-{i}"))
        models = [r.model for r in sink.records]
        assert models == ["m-0", "m-1", "m-2"]

    async def test_buffer_evicts_oldest_when_full(self) -> None:
        sink = InMemoryAuditSink(capacity=2)
        await sink.record(_record(model="a"))
        await sink.record(_record(model="b"))
        await sink.record(_record(model="c"))
        assert [r.model for r in sink.records] == ["b", "c"]

    async def test_snapshot_is_independent_of_subsequent_appends(self) -> None:
        sink = InMemoryAuditSink(capacity=10)
        await sink.record(_record(model="a"))
        snapshot = sink.records
        await sink.record(_record(model="b"))
        # The first snapshot is a tuple; a later append must not have grown it.
        assert len(snapshot) == 1
        assert len(sink.records) == 2

    def test_invalid_capacity_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            InMemoryAuditSink(capacity=0)


# ----------------------------------------------------------------- jsonl


class TestJsonlAuditSink:
    async def test_writes_one_line_per_record(self, tmp_path: Path) -> None:
        target = tmp_path / "audit.jsonl"
        sink = JsonlAuditSink(target)
        await sink.record(_record(model="m1"))
        await sink.record(_record(model="m2"))
        await sink.aclose()

        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["model"] == "m1"
        assert parsed[1]["model"] == "m2"

    async def test_creates_parent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deeper" / "audit.jsonl"
        # The parent directories do not exist yet; the sink must create them.
        sink = JsonlAuditSink(target)
        await sink.record(_record())
        await sink.aclose()
        assert target.exists()

    async def test_appends_across_sinks_pointing_at_the_same_file(
        self, tmp_path: Path
    ) -> None:
        # Models the realistic scenario where two ASGI workers share the
        # configured path. Each sink should append, never truncate.
        target = tmp_path / "audit.jsonl"
        sink_a = JsonlAuditSink(target)
        sink_b = JsonlAuditSink(target)
        await sink_a.record(_record(model="from-a"))
        await sink_b.record(_record(model="from-b"))

        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        models = {json.loads(line)["model"] for line in lines}
        assert models == {"from-a", "from-b"}

    async def test_payload_preserves_decimal_as_string(
        self, tmp_path: Path
    ) -> None:
        from decimal import Decimal

        target = tmp_path / "audit.jsonl"
        sink = JsonlAuditSink(target)
        rec = AuditRecord(
            provider=Provider.OPENAI,
            path="/v1/chat/completions",
            model="gpt-4o",
            verdict=EnforcementVerdict.ALLOWED,
            latency_ms=2.0,
            token_count=10,
            estimated_cost_usd=Decimal("0.005"),
        )
        await sink.record(rec)
        parsed = json.loads(target.read_text(encoding="utf-8").strip())
        assert parsed["estimated_cost_usd"] == "0.005"
