"""Secret scanner driver.

The scanner is stateless and thread-safe: it holds an immutable tuple of
compiled patterns and produces frozen findings. A single instance is shared
across every request worker.
"""

from __future__ import annotations

from typing import Iterable

from llm_guardrail_proxy.proxy.scanning.findings import ScanFinding
from llm_guardrail_proxy.proxy.scanning.patterns import (
    DEFAULT_SECRET_PATTERNS,
    SecretPattern,
)


def _redact(value: str) -> str:
    """Produce a triage-safe preview of a matched secret.

    The preview retains a four-character prefix and a two-character suffix
    so an operator can disambiguate which credential leaked — typically the
    prefix alone identifies the issuer (e.g. ``AKIA``, ``ghp_``) — without
    reconstructing the full secret.
    """

    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:4]}***{value[-2:]}"


class SecretScanner:
    """Run every registered pattern against an input string.

    Parameters
    ----------
    patterns:
        Iterable of :class:`SecretPattern`. Defaults to
        :data:`DEFAULT_SECRET_PATTERNS`. Callers wanting to extend or
        restrict the catalogue pass a curated subset here rather than
        mutating the module-level default.
    """

    __slots__ = ("_patterns",)

    def __init__(
        self, patterns: Iterable[SecretPattern] = DEFAULT_SECRET_PATTERNS
    ) -> None:
        self._patterns = tuple(patterns)

    @property
    def patterns(self) -> tuple[SecretPattern, ...]:
        return self._patterns

    def scan(self, text: str) -> tuple[ScanFinding, ...]:
        """Return every match found across all registered patterns.

        Order is ``(pattern declaration order, position within the text)``.
        That order is stable and deterministic so downstream redaction can
        rely on it. The method does not deduplicate overlapping matches —
        callers that need that should collapse spans themselves.
        """

        if not text:
            return ()

        findings: list[ScanFinding] = []
        for pattern in self._patterns:
            for match in pattern.regex.finditer(text):
                findings.append(
                    ScanFinding(
                        kind=pattern.name,
                        label=pattern.label,
                        severity=pattern.severity,
                        span=match.span(),
                        preview=_redact(match.group(0)),
                    )
                )
        return tuple(findings)
