"""Provider detection and prompt extraction.

Each provider speaks a slightly different request shape; this module is the
*only* place where those differences live. The rest of the proxy reasons in
terms of :class:`ParsedPrompt` and is provider-agnostic.

Adding a provider requires:

1. A new entry in :class:`Provider` (see :mod:`envelope`).
2. A new :class:`ProviderAdapter` implementation registered in
   :data:`_ADAPTERS_BY_PATH`.

The path-based dispatch table is deliberate: matching on URL prefix is
unambiguous for the three currently-supported endpoints, and avoids the
cost of parsing the body just to decide which parser to use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence

from llm_guardrail_proxy.proxy.envelope import ParsedPrompt, Provider
from llm_guardrail_proxy.proxy.exceptions import (
    PromptExtractionError,
    ProviderResolutionError,
)


class ProviderAdapter(Protocol):
    """Strategy for translating a raw request body into a ``ParsedPrompt``.

    The adapter is also responsible for the *inverse* operation
    (:meth:`redact`): producing rewritten bytes that the forwarder can ship
    upstream. Keeping both directions in one type ensures wire-format
    knowledge stays in a single module.
    """

    provider: Provider

    def parse(self, body: bytes) -> ParsedPrompt: ...

    def redact(
        self, body: bytes, replacements: Sequence[tuple[str, str]]
    ) -> bytes: ...


# ---------------------------------------------------------------- helpers


def _decode_json(body: bytes) -> Mapping[str, Any]:
    """Decode ``body`` as JSON or raise ``PromptExtractionError``.

    The error path preserves the original ``json.JSONDecodeError`` so the
    audit plane can surface byte offsets without re-parsing.
    """

    if not body:
        raise PromptExtractionError("Request body is empty.")
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PromptExtractionError("Request body is not valid JSON.") from exc

    if not isinstance(decoded, dict):
        raise PromptExtractionError(
            "Request body must be a JSON object at the top level."
        )
    return decoded


def _coerce_content(content: Any) -> str:
    """Flatten OpenAI/Anthropic multi-part content into a single string.

    The wire protocols allow ``content`` to be either a plain string or a
    list of typed parts (``{"type": "text", "text": ...}``). Tokenomics and
    PII checks operate on text, so we coalesce here once.
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                # Both providers use ``text`` for plain text parts; image
                # parts are intentionally ignored at the text-guardrail layer.
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)
    # Unknown shapes are treated as absent rather than raising — a single
    # malformed part should not block evaluation of the rest of the prompt.
    return ""


# ---------------------------------------------------------------- adapters


@dataclass(frozen=True, slots=True)
class OpenAIChatAdapter:
    """Adapter for ``POST /v1/chat/completions``."""

    provider: Provider = Provider.OPENAI

    def parse(self, body: bytes) -> ParsedPrompt:
        payload = _decode_json(body)
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            raise PromptExtractionError("Field 'model' is required.")

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise PromptExtractionError(
                "Field 'messages' must be a non-empty list."
            )

        content = "\n".join(_iter_message_text(messages))
        return ParsedPrompt(provider=self.provider, model=model, content=content)

    def redact(
        self, body: bytes, replacements: Sequence[tuple[str, str]]
    ) -> bytes:
        payload = dict(_decode_json(body))
        messages = payload.get("messages")
        if isinstance(messages, list):
            payload["messages"] = [
                {**msg, "content": _redact_content_field(msg.get("content"), replacements)}
                if isinstance(msg, dict)
                else msg
                for msg in messages
            ]
        # ``ensure_ascii=False`` preserves non-ASCII characters byte-for-byte
        # instead of escaping them; some upstreams treat the escaped form as
        # a semantically different payload.
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class AnthropicMessagesAdapter:
    """Adapter for ``POST /v1/messages``."""

    provider: Provider = Provider.ANTHROPIC

    def parse(self, body: bytes) -> ParsedPrompt:
        payload = _decode_json(body)
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            raise PromptExtractionError("Field 'model' is required.")

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise PromptExtractionError(
                "Field 'messages' must be a non-empty list."
            )

        # Anthropic carries the system prompt outside the message array.
        # Including it in the flattened content is the only way a system
        # prompt can be subjected to the same guardrails as user content.
        system = payload.get("system")
        parts: list[str] = []
        if isinstance(system, str) and system:
            parts.append(system)
        elif isinstance(system, list):
            parts.append(_coerce_content(system))

        parts.extend(_iter_message_text(messages))
        return ParsedPrompt(
            provider=self.provider,
            model=model,
            content="\n".join(p for p in parts if p),
        )

    def redact(
        self, body: bytes, replacements: Sequence[tuple[str, str]]
    ) -> bytes:
        payload = dict(_decode_json(body))

        system = payload.get("system")
        if isinstance(system, (str, list)):
            payload["system"] = _redact_content_field(system, replacements)

        messages = payload.get("messages")
        if isinstance(messages, list):
            payload["messages"] = [
                {**msg, "content": _redact_content_field(msg.get("content"), replacements)}
                if isinstance(msg, dict)
                else msg
                for msg in messages
            ]
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _iter_message_text(messages: Iterable[Any]) -> list[str]:
    """Project a chat-message array onto its textual content."""

    out: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = _coerce_content(msg.get("content"))
        if text:
            out.append(text)
    return out


def _apply_replacements(text: str, replacements: Sequence[tuple[str, str]]) -> str:
    """Apply every ``(original, new)`` substitution to ``text`` in order.

    Uses ``str.replace`` so each pair rewrites all occurrences — under-
    redaction is the dangerous failure mode, over-redaction is benign.
    """

    for original, new in replacements:
        if original:
            text = text.replace(original, new)
    return text


def _redact_content_field(
    content: Any, replacements: Sequence[tuple[str, str]]
) -> Any:
    """Rewrite a ``content`` field in place, preserving its original shape.

    OpenAI and Anthropic both allow either a plain string or a list of typed
    parts. The shape must round-trip unchanged: rewriting a string into a
    list (or vice versa) would alter the upstream's understanding of the
    request and is therefore avoided.
    """

    if isinstance(content, str):
        return _apply_replacements(content, replacements)
    if isinstance(content, list):
        rewritten: list[Any] = []
        for part in content:
            if isinstance(part, str):
                rewritten.append(_apply_replacements(part, replacements))
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                rewritten.append(
                    {**part, "text": _apply_replacements(part["text"], replacements)}
                )
            else:
                rewritten.append(part)
        return rewritten
    return content


# ---------------------------------------------------------------- dispatch


_ADAPTERS_BY_PATH: Mapping[str, ProviderAdapter] = {
    "/v1/chat/completions": OpenAIChatAdapter(),
    "/v1/messages": AnthropicMessagesAdapter(),
}


def resolve_adapter(path: str) -> ProviderAdapter:
    """Return the adapter registered for ``path``.

    Path comparison is case-sensitive (HTTP path components are) and
    trailing slashes are normalised away because gateways sometimes add them.
    """

    normalised = path.rstrip("/") or "/"
    adapter = _ADAPTERS_BY_PATH.get(normalised)
    if adapter is None:
        raise ProviderResolutionError(
            f"No provider adapter registered for path '{path}'."
        )
    return adapter


def supported_paths() -> tuple[str, ...]:
    """Return the tuple of registered guardrail-protected paths.

    Exposed for the FastAPI route layer so that route registration and
    adapter registration stay in lock-step.
    """

    return tuple(_ADAPTERS_BY_PATH.keys())
