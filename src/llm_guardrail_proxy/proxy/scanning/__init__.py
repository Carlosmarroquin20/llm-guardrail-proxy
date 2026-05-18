"""Content scanning primitives.

The package is deliberately decoupled from the proxy transport: scanners
operate on plain strings and return frozen finding records. This keeps the
detection logic reusable from non-HTTP surfaces (Phase 5's pre-commit hook)
and trivially testable in isolation.
"""

from llm_guardrail_proxy.proxy.scanning.findings import (
    ScanFinding,
    Severity,
)
from llm_guardrail_proxy.proxy.scanning.patterns import (
    DEFAULT_SECRET_PATTERNS,
    SecretPattern,
)
from llm_guardrail_proxy.proxy.scanning.secrets import SecretScanner

__all__ = [
    "DEFAULT_SECRET_PATTERNS",
    "ScanFinding",
    "SecretPattern",
    "SecretScanner",
    "Severity",
]
