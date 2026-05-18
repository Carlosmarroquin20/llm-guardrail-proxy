"""Secret-detection middleware.

Wraps :class:`SecretScanner` in the pipeline contract. The scan itself is a
sequence of compiled-regex passes; even for multi-kilobyte prompts the cost
is well under a millisecond, so no thread offload is necessary.
"""

from __future__ import annotations

from dataclasses import dataclass

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.scanning import ScanFinding, SecretScanner


@dataclass(frozen=True, slots=True)
class SecretScanMiddleware:
    """Reject prompts that contain credentials matching the curated catalogue.

    The middleware does not redact: a request that *contains* a credential
    is already suspicious — the user almost certainly did not mean to ship
    it to a third party. Surfacing the violation forces a human decision.
    Phase 3's PII follow-up will introduce a redaction variant for content
    where in-place rewriting is safer than refusal.
    """

    scanner: SecretScanner
    name: str = "secret_scan"

    async def process(self, request: ProxyRequest) -> MiddlewareOutcome:
        findings = self.scanner.scan(request.parsed.content)
        if not findings:
            return Continue(annotations={"finding_count": 0})

        return Reject(
            status_code=403,
            reason="secret_exposure_detected",
            detail=(
                f"Prompt contains {len(findings)} secret-like value(s); "
                "refusing to forward."
            ),
            annotations={
                "finding_count": len(findings),
                "findings": [_serialise(f) for f in findings],
            },
        )


def _serialise(finding: ScanFinding) -> dict:
    """Project a finding onto an audit-safe dict.

    Notably omits the raw match: ``preview`` is the only credential-derived
    field that crosses the audit boundary.
    """

    return {
        "kind": finding.kind,
        "label": finding.label,
        "severity": finding.severity.value,
        "span": list(finding.span),
        "preview": finding.preview,
    }
