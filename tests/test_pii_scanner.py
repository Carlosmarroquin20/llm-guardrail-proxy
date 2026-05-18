"""Tests for the Presidio-backed PII scanner.

The whole module is skipped if ``presidio-analyzer`` (or its spaCy model)
is not installed. This keeps the base CI green on environments that have
not opted into the [pii] extra.
"""

from __future__ import annotations

import pytest

pytest.importorskip("presidio_analyzer")

# A second guard for the spaCy model. AnalyzerEngine() construction fails
# noisily if ``en_core_web_sm`` is missing; we surface that as a skip rather
# than as a test failure so contributors get an actionable message.
try:  # noqa: SIM105 — explicit branching keeps the skip reason useful.
    import spacy  # type: ignore[import-untyped]

    spacy.load("en_core_web_sm")
except Exception:  # pragma: no cover - exercised only when model is absent
    pytest.skip(
        "spaCy model 'en_core_web_sm' is not installed; "
        "run `python -m spacy download en_core_web_sm`.",
        allow_module_level=True,
    )

from llm_guardrail_proxy.proxy.scanning import (
    DEFAULT_PII_ENTITIES,
    MissingPiiBackend,
    PiiScanner,
    ScanFinding,
    Severity,
)


@pytest.fixture(scope="module")
def scanner() -> PiiScanner:
    """Module-scoped because Presidio engine construction is ~2 s.

    Tests treat the scanner as read-only; sharing the instance is safe.
    """

    return PiiScanner()


class TestPiiScannerBasics:
    def test_clean_text_yields_no_findings(self, scanner: PiiScanner) -> None:
        assert scanner.scan("Summarise this Python module.") == ()

    def test_empty_input_yields_no_findings(self, scanner: PiiScanner) -> None:
        assert scanner.scan("") == ()

    def test_email_is_detected(self, scanner: PiiScanner) -> None:
        findings = scanner.scan("Please contact carlos.ema@example.com tomorrow.")
        labels = {f.label for f in findings}
        assert "EMAIL_ADDRESS" in labels

    def test_credit_card_is_detected(self, scanner: PiiScanner) -> None:
        # A valid-looking but synthetic Luhn-checked test PAN.
        findings = scanner.scan("Charge to 4111 1111 1111 1111 next month.")
        labels = {f.label for f in findings}
        assert "CREDIT_CARD" in labels

    def test_ip_address_is_detected(self, scanner: PiiScanner) -> None:
        findings = scanner.scan("Connection from 203.0.113.7 was rejected.")
        labels = {f.label for f in findings}
        assert "IP_ADDRESS" in labels


class TestFindingShape:
    def test_finding_is_a_scanfinding(self, scanner: PiiScanner) -> None:
        findings = scanner.scan("contact a@b.com please")
        assert findings, "expected at least one finding"
        assert isinstance(findings[0], ScanFinding)

    def test_span_indexes_back_into_original_text(self, scanner: PiiScanner) -> None:
        text = "the email a@b.com is leaked"
        findings = scanner.scan(text)
        # At least one finding must point at the email; index must be valid.
        for f in findings:
            if f.label == "EMAIL_ADDRESS":
                assert text[f.span[0] : f.span[1]] == "a@b.com"
                break
        else:
            pytest.fail("EMAIL_ADDRESS finding was not produced")

    def test_preview_does_not_contain_full_value(self, scanner: PiiScanner) -> None:
        secret = "carlos.ema@example.com"
        findings = scanner.scan(f"reach me at {secret}")
        for f in findings:
            assert secret not in f.preview

    def test_severity_is_within_expected_range(self, scanner: PiiScanner) -> None:
        findings = scanner.scan("Email: a@b.com")
        for f in findings:
            assert f.severity in {Severity.LOW, Severity.MEDIUM, Severity.HIGH}


class TestConfiguration:
    def test_invalid_threshold_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            PiiScanner(score_threshold=1.5)

    def test_custom_entity_list_is_honoured(self) -> None:
        only_email = PiiScanner(entities=("EMAIL_ADDRESS",))
        # Phone numbers must not surface when EMAIL_ADDRESS is the sole
        # registered entity.
        findings = only_email.scan("Call +1 (555) 010-1234 or email a@b.com")
        labels = {f.label for f in findings}
        assert "PHONE_NUMBER" not in labels

    def test_default_entity_set_is_non_empty(self) -> None:
        assert len(DEFAULT_PII_ENTITIES) >= 5


class TestMissingBackend:
    def test_constructor_raises_typed_error_when_presidio_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate the absence of presidio_analyzer by neutralising its
        # import: ``__import__`` returns the real module for everything
        # else, but raises for the symbol we care about.
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "presidio_analyzer":
                raise ImportError("simulated absence")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(MissingPiiBackend):
            PiiScanner()
