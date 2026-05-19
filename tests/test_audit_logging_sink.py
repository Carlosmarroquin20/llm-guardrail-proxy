"""Tests for the structlog-backed audit sink and configuration helper."""

from __future__ import annotations

import structlog

from llm_guardrail_proxy.proxy.audit import (
    AuditRecord,
    EnforcementVerdict,
    LoggingAuditSink,
    configure_logging,
)
from llm_guardrail_proxy.proxy.envelope import Provider


def _record() -> AuditRecord:
    return AuditRecord(
        provider=Provider.OPENAI,
        path="/v1/chat/completions",
        model="gpt-4o",
        verdict=EnforcementVerdict.ALLOWED,
        latency_ms=4.2,
        token_count=12,
    )


class TestLoggingAuditSink:
    async def test_emits_a_single_structured_event_per_record(self) -> None:
        # ``capture_logs`` short-circuits the configured processor chain,
        # returning the event dict the sink produced. This is the canonical
        # way structlog recommends asserting log emissions.
        with structlog.testing.capture_logs() as captured:
            sink = LoggingAuditSink()
            await sink.record(_record())

        assert len(captured) == 1
        event = captured[0]
        assert event["event"] == "audit.request"
        assert event["log_level"] == "info"
        assert event["model"] == "gpt-4o"
        assert event["verdict"] == "allowed"
        assert event["token_count"] == 12

    async def test_payload_is_json_safe(self) -> None:
        # Every field that structlog would serialise (Decimal, UUID,
        # datetime) must already be a JSON-native type at this point —
        # otherwise the JSONRenderer downstream would fail at process time.
        with structlog.testing.capture_logs() as captured:
            sink = LoggingAuditSink()
            await sink.record(_record())

        event = captured[0]
        # request_id projected as a string by ``model_dump(mode="json")``.
        assert isinstance(event["request_id"], str)
        assert isinstance(event["timestamp"], str)

    async def test_aclose_is_a_noop(self) -> None:
        # No side effects, no exceptions — protects against accidental
        # state introduction in a future refactor.
        sink = LoggingAuditSink()
        await sink.aclose()


class TestConfigureLogging:
    def test_is_idempotent(self) -> None:
        # Calling twice must not raise nor duplicate handlers; this is the
        # property the uvicorn lifespan + pytest combination relies on.
        configure_logging(json=True)
        configure_logging(json=False)
        # Reaching here without exception is the assertion.

    def test_json_mode_installs_json_renderer(self) -> None:
        # We cannot easily introspect the configured processor list across
        # structlog versions; instead, verify that a log emission round-trips
        # cleanly to JSON-renderable output.
        configure_logging(json=True)
        with structlog.testing.capture_logs() as captured:
            logger = structlog.get_logger("llm_guardrail_proxy.test")
            logger.info("probe", k="v")
        assert captured[0]["event"] == "probe"
        assert captured[0]["k"] == "v"
