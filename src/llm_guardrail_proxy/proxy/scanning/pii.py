"""PII scanner backed by Microsoft Presidio.

Presidio is a heavyweight optional dependency (it pulls in spaCy plus a
language model on the order of tens of megabytes). To keep the core proxy
installable on minimal environments, the import is deferred to scanner
construction time: importing this module never fails, but instantiating
:class:`PiiScanner` without ``presidio-analyzer`` available raises a
typed :class:`MissingPiiBackend` so the caller can degrade gracefully.

The wrapper restricts Presidio's API surface to what the proxy needs: a
single ``scan(text) -> tuple[ScanFinding, ...]`` method that mirrors
:class:`SecretScanner`. Keeping the two scanners interchangeable lets the
pipeline treat detection as a single concept regardless of backend.
"""

from __future__ import annotations

from typing import Any, Iterable

from llm_guardrail_proxy.proxy.scanning.findings import ScanFinding, Severity


class MissingPiiBackend(RuntimeError):
    """Raised when ``presidio-analyzer`` is required but not installed."""


# Default entity set. The list is intentionally narrow: every entity here is
# either personally identifying or directly regulated (PCI, financial, US
# SSNs). Broader categories like ``LOCATION`` or ``DATE_TIME`` are excluded
# because they routinely fire on benign prompt text and would generate noise
# the operator would learn to ignore — defeating the guardrail.
DEFAULT_PII_ENTITIES: tuple[str, ...] = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_SSN",
    "IP_ADDRESS",
    "PERSON",
)

# Scores below this threshold are dropped. Presidio's default is 0.0, which
# produces too many low-confidence hits. 0.5 keeps recall reasonable while
# pruning the long tail of speculative matches.
DEFAULT_SCORE_THRESHOLD: float = 0.5


def _severity_for(score: float) -> Severity:
    """Project a Presidio confidence score onto the project's severity enum.

    The thresholds are deliberately not exposed as configuration: they map
    a continuous score to a coarse severity, and Phase 3b does not have a
    use case that benefits from per-deployment tuning. Phase 4 may revisit
    this if the audit dashboard surfaces too much MEDIUM-class noise.
    """

    if score >= 0.85:
        return Severity.HIGH
    if score >= 0.6:
        return Severity.MEDIUM
    return Severity.LOW


def _preview(text: str) -> str:
    """Triage-safe preview, identical contract to ``secrets._redact``.

    PII previews are slightly more permissive — emails, phone numbers, and
    similar values are typically not as immediately actionable as a leaked
    API key. The four-char prefix / two-char suffix mask still applies.
    """

    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:4]}***{text[-2:]}"


class PiiScanner:
    """Run Presidio's analyzer engine against arbitrary text.

    Parameters
    ----------
    entities:
        Restrict detection to this entity set. Defaults to the curated
        list in :data:`DEFAULT_PII_ENTITIES`.
    score_threshold:
        Minimum confidence to surface a finding. Defaults to
        :data:`DEFAULT_SCORE_THRESHOLD`.
    language:
        Presidio language code. Only ``"en"`` is exercised by the test
        suite; switching requires a corresponding spaCy model to be
        installed.

    Raises
    ------
    MissingPiiBackend
        At construction time if ``presidio-analyzer`` is not importable.
    """

    __slots__ = ("_engine", "_entities", "_language", "_score_threshold")

    def __init__(
        self,
        *,
        entities: Iterable[str] = DEFAULT_PII_ENTITIES,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        language: str = "en",
    ) -> None:
        try:
            # Local import — see module docstring for the rationale.
            from presidio_analyzer import AnalyzerEngine
        except ImportError as exc:
            raise MissingPiiBackend(
                "PII scanning requires the 'presidio-analyzer' extra. "
                "Install with: pip install -e '.[pii]' && "
                "python -m spacy download en_core_web_sm"
            ) from exc

        if not 0.0 <= score_threshold <= 1.0:
            raise ValueError("score_threshold must be within [0.0, 1.0].")

        self._engine: Any = AnalyzerEngine()
        self._entities: tuple[str, ...] = tuple(entities)
        self._score_threshold: float = score_threshold
        self._language: str = language

    @property
    def entities(self) -> tuple[str, ...]:
        return self._entities

    def scan(self, text: str) -> tuple[ScanFinding, ...]:
        """Return findings above the configured confidence threshold."""

        if not text:
            return ()

        results = self._engine.analyze(
            text=text,
            language=self._language,
            entities=list(self._entities),
            score_threshold=self._score_threshold,
        )

        findings: list[ScanFinding] = []
        for r in results:
            matched = text[r.start : r.end]
            findings.append(
                ScanFinding(
                    kind=f"pii_{r.entity_type.lower()}",
                    label=r.entity_type,
                    severity=_severity_for(r.score),
                    span=(r.start, r.end),
                    preview=_preview(matched),
                )
            )
        return tuple(findings)
