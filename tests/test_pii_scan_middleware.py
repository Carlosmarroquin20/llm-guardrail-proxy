"""Tests for the PII-scanning middleware (BLOCK and REDACT policies)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("presidio_analyzer")

try:
    import spacy  # type: ignore[import-untyped]

    spacy.load("en_core_web_sm")
except Exception:  # pragma: no cover
    pytest.skip(
        "spaCy model 'en_core_web_sm' is not installed.",
        allow_module_level=True,
    )

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    Mutate,
    ParsedPrompt,
    Provider,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.middlewares import PiiPolicy, PiiScanMiddleware
from llm_guardrail_proxy.proxy.scanning import PiiScanner


def _envelope(content: str) -> ProxyRequest:
    body = json.dumps(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": content}],
        }
    ).encode("utf-8")
    return ProxyRequest(
        path="/v1/chat/completions",
        method="POST",
        headers={},
        raw_body=body,
        parsed=ParsedPrompt(provider=Provider.OPENAI, model="gpt-4o", content=content),
    )


@pytest.fixture(scope="module")
def scanner() -> PiiScanner:
    return PiiScanner()


class TestBlockPolicy:
    async def test_clean_prompt_continues(self, scanner: PiiScanner) -> None:
        mw = PiiScanMiddleware(scanner=scanner, policy=PiiPolicy.BLOCK)
        outcome = await mw.process(_envelope("Explain TLS handshake briefly."))
        assert isinstance(outcome, Continue)
        assert outcome.annotations["finding_count"] == 0

    async def test_email_triggers_reject(self, scanner: PiiScanner) -> None:
        mw = PiiScanMiddleware(scanner=scanner, policy=PiiPolicy.BLOCK)
        outcome = await mw.process(_envelope("reach me: a@b.com"))
        assert isinstance(outcome, Reject)
        assert outcome.status_code == 403
        assert outcome.reason == "pii_exposure_detected"


class TestRedactPolicy:
    async def test_email_triggers_mutate(self, scanner: PiiScanner) -> None:
        mw = PiiScanMiddleware(scanner=scanner, policy=PiiPolicy.REDACT)
        outcome = await mw.process(_envelope("reach me: a@b.com"))
        assert isinstance(outcome, Mutate)
        # The replacement carries the matched email and a redaction marker.
        originals = {orig for orig, _ in outcome.replacements}
        assert "a@b.com" in originals
        replacements = {new for _, new in outcome.replacements}
        assert any(r.startswith("[REDACTED:") for r in replacements)

    async def test_no_findings_yields_continue_even_in_redact_mode(
        self, scanner: PiiScanner
    ) -> None:
        mw = PiiScanMiddleware(scanner=scanner, policy=PiiPolicy.REDACT)
        outcome = await mw.process(_envelope("just a regular question"))
        assert isinstance(outcome, Continue)

    async def test_annotation_records_policy(self, scanner: PiiScanner) -> None:
        mw = PiiScanMiddleware(scanner=scanner, policy=PiiPolicy.REDACT)
        outcome = await mw.process(_envelope("contact: a@b.com"))
        assert isinstance(outcome, Mutate)
        assert outcome.annotations["policy"] == "redact"


class TestSerialisation:
    async def test_finding_payload_omits_raw_value(self, scanner: PiiScanner) -> None:
        secret = "secret-email@example.com"
        mw = PiiScanMiddleware(scanner=scanner, policy=PiiPolicy.BLOCK)
        outcome = await mw.process(_envelope(f"reach {secret}"))
        assert isinstance(outcome, Reject)
        for finding in outcome.annotations["findings"]:
            for value in finding.values():
                assert secret not in str(value)
