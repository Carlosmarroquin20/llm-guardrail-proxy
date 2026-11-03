"""Curated regex catalogue for high-confidence secret detection.

Every pattern in this module is intentionally tight: the cost of a false
positive (blocking a developer's legitimate prompt) is high, so generic
"high-entropy string" heuristics are deferred to the Phase 3 PII follow-up
where Presidio will provide context-aware analysis.

The patterns target credentials whose surface format is itself a strong
signal: distinctive prefixes, fixed lengths, or framing markers. Each rule
references the upstream issuer's published format so the regex can be
audited against the source rather than reverse-engineered from samples.

Coverage and false-positive behaviour are measured by
``evals/secret_detection``; extend that corpus when adding or changing a
pattern.
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


def _prefixed(prefix: str) -> str:
    r"""Return ``prefix`` followed by a look-behind equivalent to a leading ``\b``.

    Boundary anchors stop a pattern from matching inside a longer identifier
    that merely *contains* the prefix (``xAKIA...`` inside a random hash is
    not an AWS key). Writing that anchor as a leading ``\b`` is correct but
    slow: Python's regex engine can only jump straight to candidate
    positions when a pattern starts with a literal, so a leading ``\b``
    forces a match attempt at every character — about 50x slower per
    pattern on a 30 KB prompt. Placing the literal first and re-checking the
    preceding character with a look-behind keeps the semantics and the fast
    path. ``prefix`` must be fixed-width (equal-width alternations are fine).
    """

    return rf"{prefix}(?<!\w{prefix})"


# ``\b`` only anchors the *end* of a token that ends in a word character.
# Bodies drawn from the base64url alphabet can end in ``-``, where ``\b``
# fails against a following space or quote and the whole match is lost; those
# patterns end with an explicit look-ahead over the token alphabet instead.
_B64URL_END = r"(?![A-Za-z0-9_-])"

DEFAULT_SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        name="aws_access_key_id",
        label="AWS Access Key ID",
        # 20 chars total: ``AKIA`` (long-term key) or ``ASIA`` (temporary
        # STS key) followed by 16 base32 characters. Both authenticate.
        regex=re.compile(_prefixed("A[KS]IA") + r"[0-9A-Z]{16}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="github_pat_classic",
        label="GitHub Personal Access Token (classic)",
        regex=re.compile(_prefixed("ghp_") + r"[A-Za-z0-9]{36}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="github_pat_fine_grained",
        label="GitHub Personal Access Token (fine-grained)",
        regex=re.compile(_prefixed("github_pat_") + r"[A-Za-z0-9_]{82}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="github_oauth",
        label="GitHub OAuth / App Token",
        # ``gho_`` OAuth, ``ghu_`` user-to-server, ``ghs_`` server-to-server,
        # ``ghr_`` refresh token.
        regex=re.compile(_prefixed("gh[orsu]_") + r"[A-Za-z0-9]{36}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="openai_api_key",
        label="OpenAI API Key",
        # Two shapes:
        # * legacy user keys: ``sk-`` + 32+ alphanumerics;
        # * scoped keys (``sk-proj-``, ``sk-svcacct-``, ``sk-admin-``) with a
        #   base64url body that contains ``-`` and ``_``. A bare length rule
        #   would also match long kebab-case slugs such as
        #   ``sk-proj-dashboard-service-account-key``, so the body must also
        #   contain an upper-case letter and a digit — true of any random
        #   40+ character key, false of lower-case identifiers.
        # Anthropic's ``sk-ant-`` keys fail both branches, so they are never
        # double-reported as OpenAI keys.
        regex=re.compile(
            _prefixed("sk-")
            + r"(?:[A-Za-z0-9]{32,}\b"
            r"|(?:proj|svcacct|admin)-"
            r"(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[0-9])"
            r"[A-Za-z0-9_-]{40,}" + _B64URL_END + r")"
        ),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="anthropic_api_key",
        label="Anthropic API Key",
        regex=re.compile(
            _prefixed("sk-ant-") + r"(?:api\d{2}-|admin\d{2}-)?[A-Za-z0-9_-]{32,}" + _B64URL_END
        ),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="slack_token",
        label="Slack Token",
        # ``xox[baprs]-`` bot / user / workspace tokens, ``xapp-`` app-level
        # tokens.
        regex=re.compile(_prefixed("x(?:ox[baprs]|app)-") + r"[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="stripe_secret_key",
        label="Stripe Secret Key",
        # ``sk_`` secret and ``rk_`` restricted keys. ``pk_`` publishable keys
        # are public by design and deliberately not matched.
        regex=re.compile(_prefixed("[sr]k_(?:live|test)_") + r"[A-Za-z0-9]{24,}\b"),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="google_api_key",
        label="Google API Key",
        regex=re.compile(_prefixed("AIza") + r"[0-9A-Za-z_-]{35}" + _B64URL_END),
        severity=Severity.HIGH,
    ),
    SecretPattern(
        name="jwt",
        label="JSON Web Token",
        # Three base64url segments separated by dots; ``eyJ`` is the
        # universal prefix for a base64-encoded ``{"`` opening brace.
        regex=re.compile(
            _prefixed("eyJ")
            + r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
            + _B64URL_END
        ),
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
