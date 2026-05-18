"""Tests for the ``Mutate`` outcome handling in the pipeline.

These tests do not depend on Presidio; the mutating middleware is a fake
that returns canned replacements. The goal is to verify the pipeline
contract: a Mutate outcome rewrites the in-flight envelope via the
provider adapter and threads the rewritten envelope forward.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    Mutate,
    ParsedPrompt,
    Provider,
    ProxyRequest,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline


def _envelope(text: str) -> ProxyRequest:
    body = json.dumps(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": text}],
        }
    ).encode("utf-8")
    return ProxyRequest(
        path="/v1/chat/completions",
        method="POST",
        headers={},
        raw_body=body,
        parsed=ParsedPrompt(provider=Provider.OPENAI, model="gpt-4o", content=text),
    )


@dataclass
class _FakeMutator:
    """Middleware stub that always emits a single replacement."""

    target: str
    replacement: str
    name: str = "fake_mutator"

    async def process(self, request: ProxyRequest) -> MiddlewareOutcome:
        return Mutate(replacements=((self.target, self.replacement),))


@dataclass
class _Recorder:
    """Captures whichever envelope reaches it for downstream inspection."""

    seen: list[ProxyRequest]
    name: str = "recorder"

    async def process(self, request: ProxyRequest) -> MiddlewareOutcome:
        self.seen.append(request)
        return Continue()


class TestMutateApplication:
    async def test_mutate_rewrites_subsequent_envelope(self) -> None:
        seen: list[ProxyRequest] = []
        pipeline = MiddlewarePipeline(
            [_FakeMutator(target="secret-value", replacement="[REDACTED]"), _Recorder(seen)]
        )
        decision = await pipeline.run(_envelope("leak: secret-value here"))

        assert decision.is_allowed
        assert len(seen) == 1
        # Downstream middleware observes the redacted content.
        assert "secret-value" not in seen[0].parsed.content
        assert "[REDACTED]" in seen[0].parsed.content

    async def test_mutate_rewrites_raw_body_for_forwarder(self) -> None:
        pipeline = MiddlewarePipeline(
            [_FakeMutator(target="leak@example.com", replacement="[REDACTED:EMAIL]")]
        )
        decision = await pipeline.run(
            _envelope("contact leak@example.com for details")
        )

        rewritten = json.loads(decision.final_request.raw_body)
        message_text = rewritten["messages"][0]["content"]
        assert "leak@example.com" not in message_text
        assert "[REDACTED:EMAIL]" in message_text

    async def test_empty_replacements_leaves_envelope_unchanged(self) -> None:
        @dataclass
        class _NoOp:
            name: str = "noop"

            async def process(self, request: ProxyRequest) -> MiddlewareOutcome:
                return Mutate(replacements=())

        original = _envelope("nothing to redact")
        pipeline = MiddlewarePipeline([_NoOp()])
        decision = await pipeline.run(original)

        # The pipeline returns the same envelope (identity-equal) when the
        # mutation is a no-op. This is an efficiency property worth pinning.
        assert decision.final_request is original
