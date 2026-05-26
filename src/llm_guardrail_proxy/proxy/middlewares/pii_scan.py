"""PII-detection middleware.

Two enforcement modes are supported:

* ``BLOCK`` — emit :class:`Reject` (HTTP 403). Used when the deployment
  wants any PII to be a hard failure that requires human action.
* ``REDACT`` — emit :class:`Mutate` with token replacements such as
  ``[REDACTED:EMAIL_ADDRESS]``. The forwarder ships the redacted body and
  the upstream never sees the original PII.

The middleware itself is thread-safe and stateless beyond its constructor
parameters; the underlying Presidio engine holds spaCy state internally
and is also safe to share across requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    Mutate,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.scanning import PiiScanner


class PiiPolicy(str, Enum):
    """How the middleware responds to a PII detection."""

    BLOCK = "block"
    REDACT = "redact"


@dataclass(frozen=True, slots=True)
class PiiScanMiddleware:
    """Detect PII via Presidio and apply the configured policy."""

    scanner: PiiScanner
    policy: PiiPolicy = PiiPolicy.BLOCK
    name: str = "pii_scan"

    async def process(self, request: ProxyRequest) -> MiddlewareOutcome:
        findings = self.scanner.scan(request.parsed.content)
        if not findings:
            return Continue(annotations={"finding_count": 0})

        if self.policy is PiiPolicy.BLOCK:
            return Reject(
                status_code=403,
                reason="pii_exposure_detected",
                detail=(
                    f"Prompt contains {len(findings)} PII value(s); "
                    "refusing to forward."
                ),
                annotations={
                    "finding_count": len(findings),
                    "findings": [f.as_dict() for f in findings],
                },
            )

        # REDACT path. Replacements are built from the *original* matched
        # text — Presidio returns spans into the scanned content, and the
        # adapter applies the substitutions to every textual field of the
        # raw body. Using the matched text (rather than the span) means we
        # naturally redact every occurrence of the same PII value across
        # the multi-message body, not just the first one.
        replacements: list[tuple[str, str]] = []
        for f in findings:
            original = request.parsed.content[f.span[0] : f.span[1]]
            if not original:
                continue
            replacements.append((original, f"[REDACTED:{f.label}]"))

        return Mutate(
            replacements=tuple(replacements),
            annotations={
                "finding_count": len(findings),
                "findings": [f.as_dict() for f in findings],
                "policy": self.policy.value,
            },
        )
