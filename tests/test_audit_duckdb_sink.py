"""Tests for the DuckDB-backed audit sink.

Skipped when the optional ``duckdb`` extra is not installed.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

import duckdb  # noqa: E402  (after importorskip)

from llm_guardrail_proxy.proxy.audit import (
    AuditRecord,
    DuckdbAuditSink,
    EnforcementVerdict,
    FindingRecord,
    MissingAuditBackend,
)
from llm_guardrail_proxy.proxy.envelope import Provider


def _record(
    model: str = "gpt-4o",
    verdict: EnforcementVerdict = EnforcementVerdict.ALLOWED,
    findings: tuple[FindingRecord, ...] = (),
) -> AuditRecord:
    return AuditRecord(
        provider=Provider.OPENAI,
        path="/v1/chat/completions",
        model=model,
        verdict=verdict,
        latency_ms=1.5,
        token_count=20,
        estimated_cost_usd=Decimal("0.000123"),
        findings=findings,
    )


# ----------------------------------------------------------------- schema


class TestSchema:
    async def test_table_is_created_on_construction(self, tmp_path: Path) -> None:
        target = tmp_path / "audit.duckdb"
        sink = DuckdbAuditSink(target, table="audit_records")
        await sink.aclose()

        # Re-open with a fresh connection and verify the table exists.
        conn = duckdb.connect(str(target))
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'audit_records'"
        ).fetchall()
        conn.close()
        assert rows == [("audit_records",)]

    async def test_creates_parent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deeper" / "audit.duckdb"
        sink = DuckdbAuditSink(target)
        await sink.aclose()
        assert target.exists()

    def test_invalid_table_name_is_rejected(self, tmp_path: Path) -> None:
        # The table name is interpolated into DDL, so it must be a tight
        # whitelist. The validator catches SQL-injection-shaped values.
        with pytest.raises(ValueError):
            DuckdbAuditSink(tmp_path / "x.duckdb", table="audit; DROP TABLE x")


# ----------------------------------------------------------------- writes


class TestPersistence:
    async def test_record_is_persisted_and_queryable(self, tmp_path: Path) -> None:
        target = tmp_path / "audit.duckdb"
        sink = DuckdbAuditSink(target)

        rec = _record(model="gpt-4o-mini")
        await sink.record(rec)
        await sink.aclose()

        conn = duckdb.connect(str(target))
        rows = conn.execute(
            "SELECT request_id, model, verdict, token_count, "
            "estimated_cost_usd, latency_ms FROM audit_records"
        ).fetchall()
        conn.close()

        assert len(rows) == 1
        rid, model, verdict, tok, cost, latency = rows[0]
        assert rid == str(rec.request_id)
        assert model == "gpt-4o-mini"
        assert verdict == "allowed"
        assert tok == 20
        # DuckDB returns Decimal for DECIMAL columns; equality preserves
        # the no-float-drift contract that motivated the choice.
        assert cost == Decimal("0.000123")
        assert latency == pytest.approx(1.5)

    async def test_findings_are_stored_as_json(self, tmp_path: Path) -> None:
        target = tmp_path / "audit.duckdb"
        sink = DuckdbAuditSink(target)
        rec = _record(
            verdict=EnforcementVerdict.REJECTED,
            findings=(
                FindingRecord(
                    scanner="secret_scan",
                    kind="aws_access_key_id",
                    label="AWS Access Key ID",
                    severity="high",
                    preview="AKIA***OP",
                ),
            ),
        )
        await sink.record(rec)
        await sink.aclose()

        conn = duckdb.connect(str(target))
        (findings_json,) = conn.execute(
            "SELECT findings FROM audit_records"
        ).fetchone()
        # DuckDB's JSON column returns the value as a string; downstream
        # consumers parse it. We assert the round-trip survives intact.
        import json

        parsed = json.loads(findings_json)
        conn.close()
        assert parsed[0]["kind"] == "aws_access_key_id"
        assert parsed[0]["preview"] == "AKIA***OP"

    async def test_multiple_records_accumulate(self, tmp_path: Path) -> None:
        target = tmp_path / "audit.duckdb"
        sink = DuckdbAuditSink(target)
        for i in range(5):
            await sink.record(_record(model=f"m-{i}"))
        await sink.aclose()

        conn = duckdb.connect(str(target))
        count = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
        conn.close()
        assert count == 5

    async def test_in_memory_path_is_supported(self) -> None:
        # ``:memory:`` is the canonical DuckDB ephemeral path; the sink
        # must work without touching disk, which is the contract every
        # in-process test relies on.
        sink = DuckdbAuditSink(":memory:")
        await sink.record(_record())
        await sink.aclose()


# ---------------------------------------------------------------- backend


class TestMissingBackend:
    def test_constructor_raises_typed_error_when_duckdb_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "duckdb":
                raise ImportError("simulated absence")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(MissingAuditBackend):
            DuckdbAuditSink(tmp_path / "x.duckdb")
