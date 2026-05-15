"""Tests for the async circuit breaker."""

from __future__ import annotations

import pytest

from llm_guardrail_proxy.proxy.circuit_breaker import BreakerState, CircuitBreaker
from llm_guardrail_proxy.proxy.exceptions import CircuitOpenError


class _Clock:
    """Manual monotonic clock for deterministic breaker tests."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class TestCircuitBreaker:
    async def test_successful_call_keeps_state_closed(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, reset_seconds=10)

        async def ok() -> int:
            return 42

        assert await breaker.call(ok) == 42
        assert breaker.state is BreakerState.CLOSED

    async def test_opens_after_threshold_consecutive_failures(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(failure_threshold=3, reset_seconds=10, clock=clock)

        async def boom() -> None:
            raise RuntimeError("fail")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await breaker.call(boom)

        assert breaker.state is BreakerState.OPEN

    async def test_open_breaker_short_circuits_subsequent_calls(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)

        async def boom() -> None:
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await breaker.call(boom)

        async def never_invoked() -> None:
            raise AssertionError("should not run while breaker is open")

        with pytest.raises(CircuitOpenError):
            await breaker.call(never_invoked)

    async def test_half_open_probe_on_success_restores_closed(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)

        async def boom() -> None:
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN

        clock.advance(15)  # past the reset window

        async def ok() -> str:
            return "recovered"

        result = await breaker.call(ok)
        assert result == "recovered"
        assert breaker.state is BreakerState.CLOSED

    async def test_half_open_probe_failure_reopens_breaker(self) -> None:
        clock = _Clock()
        breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)

        async def boom() -> None:
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        clock.advance(15)

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN

    def test_invalid_construction_arguments_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(failure_threshold=0)
        with pytest.raises(ValueError):
            CircuitBreaker(reset_seconds=0)
