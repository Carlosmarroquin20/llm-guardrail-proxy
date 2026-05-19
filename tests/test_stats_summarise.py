"""Unit tests for the pure ``summarise`` aggregator."""

from __future__ import annotations

from decimal import Decimal

from llm_guardrail_proxy.proxy.audit import (
    AuditRecord,
    EnforcementVerdict,
    FindingRecord,
)
from llm_guardrail_proxy.proxy.envelope import Provider
from llm_guardrail_proxy.proxy.stats import StatsSummary, summarise


def _rec(
    *,
    model: str = "gpt-4o",
    verdict: EnforcementVerdict = EnforcementVerdict.ALLOWED,
    rejecting_middleware: str | None = None,
    estimated_cost_usd: Decimal | None = None,
    latency_ms: float = 1.0,
    findings: tuple[FindingRecord, ...] = (),
) -> AuditRecord:
    return AuditRecord(
        provider=Provider.OPENAI,
        path="/v1/chat/completions",
        model=model,
        verdict=verdict,
        rejecting_middleware=rejecting_middleware,
        estimated_cost_usd=estimated_cost_usd,
        latency_ms=latency_ms,
        findings=findings,
    )


def _finding(scanner: str, kind: str = "x") -> FindingRecord:
    return FindingRecord(
        scanner=scanner,
        kind=kind,
        label=kind.upper(),
        severity="high",
        preview="****",
    )


# ----------------------------------------------------------------- empty


class TestEmptyInput:
    def test_zero_records_produces_zeroed_summary(self) -> None:
        summary = summarise([])
        assert isinstance(summary, StatsSummary)
        assert summary.total_requests == 0
        assert summary.allowed == 0
        assert summary.rejected == 0
        assert summary.rejection_rate == 0.0
        assert summary.total_estimated_cost_usd == Decimal("0")
        assert summary.avg_latency_ms == 0.0
        assert summary.rejections_by_middleware == {}
        assert summary.requests_by_model == {}
        assert summary.findings_by_scanner == {}


# ----------------------------------------------------------------- shape


class TestVerdictCounts:
    def test_all_allowed(self) -> None:
        summary = summarise([_rec(), _rec(), _rec()])
        assert summary.total_requests == 3
        assert summary.allowed == 3
        assert summary.rejected == 0
        assert summary.rejection_rate == 0.0

    def test_all_rejected(self) -> None:
        summary = summarise(
            [
                _rec(
                    verdict=EnforcementVerdict.REJECTED,
                    rejecting_middleware="tokenomics",
                )
            ]
            * 4
        )
        assert summary.total_requests == 4
        assert summary.allowed == 0
        assert summary.rejected == 4
        assert summary.rejection_rate == 1.0

    def test_mixed_rejection_rate(self) -> None:
        records = [
            _rec(),  # allowed
            _rec(),  # allowed
            _rec(
                verdict=EnforcementVerdict.REJECTED,
                rejecting_middleware="secret_scan",
            ),
        ]
        summary = summarise(records)
        assert summary.allowed == 2
        assert summary.rejected == 1
        # Float comparison via direct equality is safe here: 1/3 has the
        # same canonical form regardless of accumulation order.
        assert summary.rejection_rate == 1 / 3


# --------------------------------------------------------------- monetary


class TestCostAccumulation:
    def test_decimal_accumulation_is_exact(self) -> None:
        # Pick values whose sum would suffer measurable float drift: 0.1
        # has no exact binary representation. Decimal must accumulate
        # exactly the right total.
        records = [
            _rec(estimated_cost_usd=Decimal("0.1")) for _ in range(10)
        ]
        summary = summarise(records)
        assert summary.total_estimated_cost_usd == Decimal("1.0")

    def test_none_cost_is_treated_as_zero(self) -> None:
        records = [
            _rec(estimated_cost_usd=Decimal("0.5")),
            _rec(estimated_cost_usd=None),
        ]
        summary = summarise(records)
        assert summary.total_estimated_cost_usd == Decimal("0.5")


# --------------------------------------------------------------- latency


class TestLatencyAverage:
    def test_average_over_all_records(self) -> None:
        records = [
            _rec(latency_ms=1.0),
            _rec(latency_ms=2.0),
            _rec(latency_ms=3.0),
        ]
        assert summarise(records).avg_latency_ms == 2.0


# ----------------------------------------------------------- groupings


class TestGroupings:
    def test_rejections_grouped_by_middleware(self) -> None:
        records = [
            _rec(
                verdict=EnforcementVerdict.REJECTED,
                rejecting_middleware="secret_scan",
            ),
            _rec(
                verdict=EnforcementVerdict.REJECTED,
                rejecting_middleware="secret_scan",
            ),
            _rec(
                verdict=EnforcementVerdict.REJECTED,
                rejecting_middleware="tokenomics",
            ),
            _rec(),  # allowed — must not appear in the counter
        ]
        summary = summarise(records)
        assert summary.rejections_by_middleware == {
            "secret_scan": 2,
            "tokenomics": 1,
        }

    def test_requests_grouped_by_model(self) -> None:
        records = [_rec(model="gpt-4o"), _rec(model="gpt-4o"), _rec(model="gpt-4")]
        summary = summarise(records)
        assert summary.requests_by_model == {"gpt-4o": 2, "gpt-4": 1}

    def test_findings_grouped_by_scanner(self) -> None:
        records = [
            _rec(findings=(_finding("secret_scan"), _finding("pii_scan"))),
            _rec(findings=(_finding("secret_scan"),)),
        ]
        summary = summarise(records)
        assert summary.findings_by_scanner == {
            "secret_scan": 2,
            "pii_scan": 1,
        }

    def test_grouping_ordering_is_descending_by_count(self) -> None:
        records = (
            [_rec(model="a")] * 3
            + [_rec(model="b")] * 5
            + [_rec(model="c")] * 1
        )
        # Iteration order on the resulting dict must be by descending count.
        # ``Counter.most_common`` underwrites this, but pinning the contract
        # here guards against a refactor that loses the property.
        models = list(summarise(records).requests_by_model.keys())
        assert models == ["b", "a", "c"]
