"""Unit tests for the regex-based secret scanner.

The suite exercises every catalogued pattern with a positive sample built
from the issuer's published format, plus a negative case to catch over-eager
regexes. The fixtures use deliberately fake values: the prefixes are
authentic but the bodies are random alphanumerics that cannot be mistaken
for live credentials.
"""

from __future__ import annotations

import pytest

from llm_guardrail_proxy.proxy.scanning import (
    DEFAULT_SECRET_PATTERNS,
    ScanFinding,
    SecretScanner,
    Severity,
)
from llm_guardrail_proxy.proxy.scanning.secrets import _redact


# Synthetic fixtures — each value has the correct shape but a deterministic,
# non-secret body. ``AKIA...`` is the only fragment AWS treats as sensitive
# (the surrounding random letters make the value structurally valid).
_SAMPLES: dict[str, str] = {
    "aws_access_key_id": "AKIAABCDEFGHIJKLMNOP",
    "github_pat_classic": "ghp_" + "a" * 36,
    "github_pat_fine_grained": "github_pat_" + "B" * 82,
    "github_oauth": "ghs_" + "c" * 36,
    "openai_api_key": "sk-" + "d" * 48,
    "anthropic_api_key": "sk-ant-api03-" + "e" * 64,
    "slack_token": "xoxb-1111111111-2222222222-AbCdEf",
    "stripe_secret_key": "sk_live_" + "0" * 24,
    "google_api_key": "AIza" + "Z" * 35,
    "jwt": "eyJabcdefgh.aaaaaaaa.bbbbbbbb",
    "pem_private_key": "-----BEGIN RSA PRIVATE KEY-----",
}


# --------------------------------------------------------------- redaction


class TestRedact:
    def test_long_value_keeps_prefix_and_suffix(self) -> None:
        assert _redact("AKIAABCDEFGHIJKLMNOP") == "AKIA***OP"

    def test_short_value_is_fully_masked(self) -> None:
        assert _redact("abc") == "***"

    def test_boundary_length_six_is_fully_masked(self) -> None:
        # ``len <= 6`` is the documented threshold for full masking.
        assert _redact("abcdef") == "******"


# --------------------------------------------------------------- catalogue


class TestPatternCatalogue:
    def test_every_pattern_has_a_unique_name(self) -> None:
        names = [p.name for p in DEFAULT_SECRET_PATTERNS]
        assert len(names) == len(set(names))

    def test_catalogue_is_non_empty(self) -> None:
        assert len(DEFAULT_SECRET_PATTERNS) >= 10

    def test_every_sample_is_paired_with_a_pattern(self) -> None:
        # Guards against catalogue drift: if a new pattern is added without
        # a sample, this fails — forcing the test author to extend coverage.
        registered = {p.name for p in DEFAULT_SECRET_PATTERNS}
        assert set(_SAMPLES.keys()) == registered


# --------------------------------------------------------------- positive


@pytest.mark.parametrize("pattern_name,sample", list(_SAMPLES.items()))
def test_each_pattern_matches_its_synthetic_sample(
    pattern_name: str, sample: str
) -> None:
    findings = SecretScanner().scan(f"prefix {sample} suffix")
    matched_kinds = {f.kind for f in findings}
    assert pattern_name in matched_kinds, (
        f"Pattern '{pattern_name}' failed to match its synthetic sample."
    )


# --------------------------------------------------------------- negative


class TestNegativeCases:
    def test_empty_text_yields_no_findings(self) -> None:
        assert SecretScanner().scan("") == ()

    def test_innocuous_prompt_yields_no_findings(self) -> None:
        text = (
            "Summarise the architecture of a Python microservice using "
            "FastAPI and SQLAlchemy. Highlight common pitfalls."
        )
        assert SecretScanner().scan(text) == ()

    def test_lookalike_identifier_does_not_match_aws(self) -> None:
        # ``AKIA`` followed by 15 chars (one short of the AWS spec) must not
        # trigger — this is the regression case for the word-boundary anchor.
        assert SecretScanner().scan("AKIAABCDEFGHIJKLMN") == ()

    def test_short_sk_prefix_is_ignored(self) -> None:
        # ``sk-shortvalue`` is a common Stripe placeholder in tutorials; we
        # require ≥32 body chars to avoid blocking docs/snippets.
        assert SecretScanner().scan("sk-shortvalue") == ()


# --------------------------------------------------------------- composite


class TestCompositeBehaviour:
    def test_multiple_findings_are_all_reported(self) -> None:
        text = f"key1={_SAMPLES['aws_access_key_id']} key2={_SAMPLES['github_pat_classic']}"
        findings = SecretScanner().scan(text)
        kinds = {f.kind for f in findings}
        assert "aws_access_key_id" in kinds
        assert "github_pat_classic" in kinds

    def test_finding_preview_does_not_contain_full_secret(self) -> None:
        secret = _SAMPLES["aws_access_key_id"]
        findings = SecretScanner().scan(secret)
        assert len(findings) == 1
        finding = findings[0]
        assert isinstance(finding, ScanFinding)
        assert secret not in finding.preview
        # ``preview`` retains issuer prefix for triage value.
        assert finding.preview.startswith("AKIA")

    def test_span_offsets_locate_match_in_original_text(self) -> None:
        prefix = "leading text "
        text = prefix + _SAMPLES["aws_access_key_id"]
        findings = SecretScanner().scan(text)
        start, end = findings[0].span
        assert text[start:end] == _SAMPLES["aws_access_key_id"]

    def test_custom_pattern_list_is_honoured(self) -> None:
        only_aws = SecretScanner(
            patterns=[p for p in DEFAULT_SECRET_PATTERNS if p.name == "aws_access_key_id"]
        )
        text = f"{_SAMPLES['aws_access_key_id']} and {_SAMPLES['github_pat_classic']}"
        findings = only_aws.scan(text)
        kinds = {f.kind for f in findings}
        assert kinds == {"aws_access_key_id"}


# --------------------------------------------------------------- severity


class TestSeverityOrdering:
    def test_severity_supports_ordinal_comparison(self) -> None:
        assert Severity.LOW < Severity.MEDIUM < Severity.HIGH

    def test_severity_serialises_as_string(self) -> None:
        # Inherited from ``str``; protects the audit-log contract.
        assert Severity.HIGH == "high"
