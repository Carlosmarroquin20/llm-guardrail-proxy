"""Typed request envelope and middleware outcome sum-type.

The envelope is the single object that flows through the middleware chain.
Phase 3 middlewares (PII redaction) will produce *mutated* envelopes; Phase 2
defines only the read-only and short-circuit branches so the contract is
stable enough to extend without breaking changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Provider(str, Enum):
    """Recognised upstream providers.

    Adding a new provider here is the first step in extending coverage; the
    matching adapter in :mod:`llm_guardrail_proxy.proxy.providers` is the
    second. Both must change together — the type system makes the omission
    explicit.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


@dataclass(frozen=True, slots=True)
class ParsedPrompt:
    """Normalised view of a provider-specific request body.

    ``content`` is the flattened, human-readable prompt text used by every
    text-oriented middleware (tokenomics today, PII tomorrow). ``model`` is
    extracted up-front so that pricing and policy decisions never have to
    re-parse the raw body.
    """

    provider: Provider
    model: str
    content: str


@dataclass(frozen=True, slots=True)
class ProxyRequest:
    """Immutable envelope handed to every middleware.

    Notes
    -----
    The raw body is preserved separately from the parsed view so that the
    forwarder can replay the exact bytes the client sent — re-serialising
    from the parsed view would risk dropping fields the proxy does not yet
    understand.
    """

    path: str
    method: str
    headers: Mapping[str, str]
    raw_body: bytes
    parsed: ParsedPrompt
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------- outcomes


@dataclass(frozen=True, slots=True)
class Continue:
    """Signals the pipeline to proceed to the next middleware.

    Carries an optional ``annotations`` map so middlewares can attach
    diagnostic data (e.g. cost estimate) that downstream consumers — or the
    audit plane in Phase 4 — can inspect without re-running the analysis.
    """

    annotations: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Reject:
    """Signals the pipeline to short-circuit with a structured rejection.

    ``status_code`` is the HTTP status surfaced to the client. ``reason`` is
    a short machine-readable identifier; ``detail`` is free-form human text.
    """

    status_code: int
    reason: str
    detail: str
    annotations: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Mutate:
    """Signals the pipeline to rewrite the request body and continue.

    ``replacements`` is an ordered list of ``(original, replacement)`` text
    substitutions. The pipeline applies them to every textual field of the
    request body — via the provider adapter, so that wire-format details
    stay encapsulated. ``str.replace`` semantics apply: every occurrence is
    rewritten, which is the safer bias (over-redaction never leaks).

    A Mutate outcome leaves the pipeline contract intact: subsequent
    middlewares see the rewritten envelope, and the final upstream call
    receives the rewritten body.
    """

    replacements: tuple[tuple[str, str], ...]
    annotations: Mapping[str, Any] = field(default_factory=dict)


MiddlewareOutcome = Continue | Reject | Mutate
"""Algebraic outcome a middleware may produce."""
