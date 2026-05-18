"""Curated regex catalogue for high-confidence secret detection.

Every pattern in this module is intentionally tight: the cost of a false
positive (blocking a developer's legitimate prompt) is high, so generic
"high-entropy string" heuristics are deferred to the Phase 3 PII follow-up
where Presidio will provide context-aware analysis.

The patterns target credentials whose surface format is itself a strong
signal: distinctive prefixes, fixed lengths, or framing markers. Each rule
references the upstream issuer's published format so the regex can be
audited against the source rather than reverse-engineered from samples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from llm_guardrail_proxy.proxy.scanning.findings import Severity


@dataclass(frozen=True, slots=True)
class SecretPattern:
    """A single rule in the secret-detection catalogue.

    ``regex`` is stored pre-compiled to keep ``SecretScanner.scan`` an O(n)
    pass per pattern; compiling on every call would dominate runtime on
    short prompts.
    """

    name: str
    label: str
    regex: re.Pattern[str]
    severity: Severity


# The trailing ``\b`` word-boundary anchors are deliberate — they prevent the
# patterns from matching inside longer identifiers that merely *contain* the
# prefix (e.g. ``AKIAB...`` embedded in a 40-character random hash that is
# not, in fact, an AWS key).
DEFAULT_SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        name="aws_access_key_id",
        label="AWS Access Key ID",
        # AWS access keys are 20 chars total, fixed ``AKIA`` prefix.
        regex=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="github_pat_classic",
        label="GitHub Personal Access Token (classic)",
        regex=re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="github_pat_fine_grained",
        label="GitHub Personal Access Token (fine-grained)",
        regex=re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="github_oauth",
        label="GitHub OAuth / App Token",
        regex=re.compile(r"\bgh[osu]_[A-Za-z0-9]{36}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="openai_api_key",
        label="OpenAI API Key",
        # Covers both legacy ``sk-`` and project-scoped ``sk-proj-`` keys.
        # The 32-char minimum trades a small recall loss for far fewer
        # false positives against identifiers that merely start with ``sk-``.
        regex=re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="anthropic_api_key",
        label="Anthropic API Key",
        regex=re.compile(r"\bsk-ant-(?:api\d{2}-)?[A-Za-z0-9_-]{32,}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="slack_token",
        label="Slack Token",
        regex=re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="stripe_secret_key",
        label="Stripe Secret Key",
        regex=re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{24,}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="google_api_key",
        label="Google API Key",
        regex=re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="jwt",
        label="JSON Web Token",
        # Three base64url segments separated by dots; ``eyJ`` is the
        # universal prefix for a base64-encoded ``{"`` opening brace.
        regex=re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        severity=Severity.MEDIUM,
    ),
    SecretPattern(
        name="pem_private_key",
        label="PEM-encoded Private Key",
        # Multiline-aware match; the framing markers are the high-confidence
        # signal so the body characters can stay permissive.
        regex=re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"
        ),
        severity=Severity.HIGH,
    ),
)
