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


def _body(n: int, alphabet: str = "aB3cD5eF7gH9jK2mN4pQ6rS8tU") -> str:
    """Deterministic, non-repeating filler of length ``n``.

    Bodies like ``"a" * 36`` are deliberately treated as placeholders by the
    scanner, so fixtures cycle through a mixed alphabet instead.
    """

    return (alphabet * (n // len(alphabet) + 1))[:n]


# Synthetic fixtures — each value has the correct shape but a deterministic,
# non-secret body.
_SAMPLES: dict[str, str] = {
    "aws_access_key_id": "AKIAABCDEFGHIJKLMNOP",
    "github_pat_classic": "ghp_" + _body(36),
    "github_pat_fine_grained": "github_pat_" + _body(82),
    "github_oauth": "ghs_" + _body(36),
    "openai_api_key": "sk-" + _body(48),
    "anthropic_api_key": "sk-ant-api03-" + _body(64),
    "slack_token": "xoxb-1234567890-0987654321-AbCdEf",
    "stripe_secret_key": "sk_live_" + _body(24),
    "google_api_key": "AIza" + _body(35),
    "jwt": "eyJabcdefgh.abcdefgh.ijklmnop",
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

    def test_no_pattern_starts_with_a_boundary_assertion(self) -> None:
        # A leading ``\b`` or look-behind disables the regex engine's
        # literal-prefix search and makes each pattern ~50x slower on large
        # prompts. Use ``_prefixed`` instead (see patterns.py).
        for p in DEFAULT_SECRET_PATTERNS:
            assert not p.regex.pattern.startswith(("\\b", "(?<")), p.name

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

    @pytest.mark.parametrize("before", ["x", "7", "_", "é"])
    def test_prefix_glued_to_a_word_character_is_ignored(self, before: str) -> None:
        # Same semantics as a leading ``\b``: a word character immediately
        # before the prefix means the value is part of a longer identifier.
        assert SecretScanner().scan(before + _SAMPLES["aws_access_key_id"]) == ()

    @pytest.mark.parametrize("before", ["", " ", "=", "'", '"', "(", ":", "-", "\n"])
    def test_prefix_after_a_separator_is_detected(self, before: str) -> None:
        findings = SecretScanner().scan(before + _SAMPLES["aws_access_key_id"])
        assert [f.kind for f in findings] == ["aws_access_key_id"]

    def test_long_lowercase_slug_behind_scoped_prefix_is_ignored(self) -> None:
        # Long enough for a pure length rule, but no upper-case letter or
        # digit: an identifier, not a key.
        slug = "sk-proj-dashboard-service-account-rotation-billing-export"
        assert SecretScanner().scan(f"see {slug} for details") == ()

    def test_stripe_publishable_key_is_ignored(self) -> None:
        assert SecretScanner().scan("pk_live_" + _body(30)) == ()


# --------------------------------------------------------------- format coverage


class TestFormatVariants:
    """Variants surfaced by ``evals/secret_detection``; each was missed before."""

    @pytest.mark.parametrize(
        "kind,value",
        [
            ("aws_access_key_id", "ASIAABCDEFGHIJKLMNOP"),
            ("github_oauth", "ghr_" + _body(36)),
            ("openai_api_key", "sk-proj-" + _body(60, "aB3_-cD5eF7") + "T3BlbkFJ" + _body(60)),
            ("openai_api_key", "sk-svcacct-" + _body(60, "aB3_-cD5eF7")),
            ("openai_api_key", "sk-admin-" + _body(60, "aB3_-cD5eF7")),
            ("slack_token", "xapp-1-A0123456789-1234567890123-" + _body(64, "0123456789abcdef")),
            ("stripe_secret_key", "rk_live_" + _body(30)),
        ],
    )
    def test_variant_is_detected(self, kind: str, value: str) -> None:
        assert kind in {f.kind for f in SecretScanner().scan(f"token: {value}\n")}

    def test_token_ending_in_hyphen_is_detected(self) -> None:
        # ``\b`` cannot anchor after ``-``; the look-ahead boundary must.
        value = "AIza" + _body(34) + "-"
        findings = SecretScanner().scan(f'key = "{value}" ')
        assert [f.kind for f in findings] == ["google_api_key"]

    def test_anthropic_key_is_not_double_reported_as_openai(self) -> None:
        value = "sk-ant-api03-" + _body(93, "aB3_-cD5eF7") + "AA"
        assert {f.kind for f in SecretScanner().scan(value)} == {"anthropic_api_key"}


# --------------------------------------------------------------- placeholders


class TestPlaceholders:
    @pytest.mark.parametrize(
        "value",
        [
            "AKIA" + "X" * 16,
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_" + "x" * 36,
            "sk-" + "x" * 48,
            "sk_live_" + "0" * 24,
        ],
    )
    def test_placeholder_is_skipped_by_default(self, value: str) -> None:
        assert SecretScanner().scan(f"set it to {value}") == ()

    def test_placeholders_are_reported_when_skipping_is_disabled(self) -> None:
        scanner = SecretScanner(skip_placeholders=False)
        assert {f.kind for f in scanner.scan("AKIA" + "X" * 16)} == {"aws_access_key_id"}

    def test_real_shaped_key_next_to_placeholder_is_still_reported(self) -> None:
        text = f"old: {'AKIA' + 'X' * 16}, new: {_SAMPLES['aws_access_key_id']}"
        findings = SecretScanner().scan(text)
        assert len(findings) == 1
        assert findings[0].kind == "aws_access_key_id"


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
