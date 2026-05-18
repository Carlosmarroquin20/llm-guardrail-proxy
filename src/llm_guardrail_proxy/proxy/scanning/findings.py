"""Value objects produced by content scanners.

Findings deliberately never carry the raw matched string. The whole point of
the guardrail is to prevent secret exfiltration; embedding the secret in a
finding object — which is then logged, serialised, and audited — would
re-leak it through the diagnostic plane. ``preview`` carries a redacted
fragment that is sufficient for triage without restoring the full value.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    """Ordinal severity classifier shared across all scanners.

    Subclasses ``str`` so values serialise as plain JSON strings in audit
    records without a custom encoder. The ``__lt__`` override lets callers
    sort findings by severity for human-readable reports.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank < other.rank


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
}


@dataclass(frozen=True, slots=True)
class ScanFinding:
    """A single detection emitted by a scanner.

    Attributes
    ----------
    kind:
        Machine-readable identifier for the rule that fired. Stable across
        releases so audit consumers can build dashboards keyed on it.
    label:
        Human-readable description, intended for operator-facing surfaces.
    severity:
        Triage priority. ``HIGH`` blocks by default; ``LOW`` is informational.
    span:
        ``(start, end)`` offsets into the scanned text. Useful for downstream
        redaction (Phase 3 PII step) without re-running the regex.
    preview:
        Truncated, irreversibly-redacted glimpse of the matched fragment.
        Never includes more than the first four and last two characters of
        the original match.
    """

    kind: str
    label: str
    severity: Severity
    span: tuple[int, int]
    preview: str
