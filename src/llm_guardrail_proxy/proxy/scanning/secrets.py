"""Secret scanner driver.

The scanner is stateless and thread-safe: it holds an immutable tuple of
compiled patterns and produces frozen findings. A single instance is shared
across every request worker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from llm_guardrail_proxy.proxy.scanning.findings import ScanFinding
from llm_guardrail_proxy.proxy.scanning.patterns import (
    DEFAULT_SECRET_PATTERNS,
    SecretPattern,
)

# Eight identical characters in a row. A random 16+ character credential body
# essentially never contains one (for a 62-symbol alphabet the odds are on the
# order of 1e-12 per position), while placeholders such as
# ``AKIAXXXXXXXXXXXXXXXX`` or ``sk-xxxxxxxx...`` always do.
_REPEATED_RUN = re.compile(r"(.)\1{7}")


def _is_placeholder(value: str) -> bool:
    """Return True for credential-shaped values that are obviously not real.

    Developers paste documentation snippets into prompts ("where do I put
    AKIAXXXXXXXXXXXXXXXX?"). Refusing those trains people to bypass the
    proxy, and blocking them protects nothing. Two signals are used, both
    chosen so that a randomly generated key cannot plausibly trigger them:
    a long run of one repeated character, and the literal ``EXAMPLE`` that
    vendors (AWS in particular) put in their documentation keys.
    """

    return _REPEATED_RUN.search(value) is not None or "EXAMPLE" in value.upper()


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
    skip_placeholders:
        Drop matches that are obviously documentation placeholders (see
        :func:`_is_placeholder`). On by default; turn it off to audit how
        often placeholders appear.
    """

    __slots__ = ("_patterns", "_skip_placeholders")

    def __init__(
        self,
        patterns: Iterable[SecretPattern] = DEFAULT_SECRET_PATTERNS,
        *,
        skip_placeholders: bool = True,
    ) -> None:
        self._patterns = tuple(patterns)
        self._skip_placeholders = skip_placeholders

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
                if self._skip_placeholders and _is_placeholder(match.group(0)):
                    continue
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
