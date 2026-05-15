"""Unit tests for the middleware pipeline orchestrator."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    MiddlewareOutcome,
    ParsedPrompt,
    Provider,
    ProxyRequest,
    Reject,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline


def _envelope() -> ProxyRequest:
    return ProxyRequest(
        path="/v1/chat/completions",
        method="POST",
        headers={},
        raw_body=b"{}",
        parsed=ParsedPrompt(provider=Provider.OPENAI, model="gpt-4o", content="hi"),
    )


@dataclass
class _Recording:
    """Stub middleware that records invocation order and returns a canned outcome."""

    name: str
    outcome: MiddlewareOutcome
    log: list[str]

    async def process(self, request: ProxyRequest) -> MiddlewareOutcome:
        self.log.append(self.name)
        return self.outcome


class TestPipelineRun:
    async def test_all_continue_yields_allowed_decision(self) -> None:
        log: list[str] = []
        pipeline = MiddlewarePipeline(
            [
                _Recording("a", Continue(annotations={"k": 1}), log),
                _Recording("b", Continue(annotations={"k": 2}), log),
            ]
        )
        decision = await pipeline.run(_envelope())
        assert decision.is_allowed
        assert log == ["a", "b"]
        assert decision.annotations == {"a": {"k": 1}, "b": {"k": 2}}
        assert decision.rejecting_middleware is None

    async def test_reject_short_circuits_chain(self) -> None:
        log: list[str] = []
        pipeline = MiddlewarePipeline(
            [
                _Recording("a", Continue(), log),
                _Recording(
                    "b",
                    Reject(status_code=413, reason="too_big", detail="nope"),
                    log,
                ),
                _Recording("c", Continue(), log),
            ]
        )
        decision = await pipeline.run(_envelope())
        assert not decision.is_allowed
        assert log == ["a", "b"]
        assert decision.rejecting_middleware == "b"
        assert isinstance(decision.outcome, Reject)
        assert decision.outcome.reason == "too_big"

    async def test_empty_pipeline_accepts_everything(self) -> None:
        decision = await MiddlewarePipeline([]).run(_envelope())
        assert decision.is_allowed
        assert decision.annotations == {}

    def test_duplicate_names_are_rejected_at_construction(self) -> None:
        log: list[str] = []
        with pytest.raises(ValueError):
            MiddlewarePipeline(
                [
                    _Recording("dup", Continue(), log),
                    _Recording("dup", Continue(), log),
                ]
            )
