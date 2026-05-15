"""Minimal async circuit breaker for the upstream forwarder.

The implementation follows the canonical three-state machine
(``CLOSED`` → ``OPEN`` → ``HALF_OPEN`` → ``CLOSED``). It is intentionally
small: a more elaborate breaker (sliding-window error rate, jitter, etc.)
is justified only at scale Phase 2 does not target.

Time is sourced from a callable so tests can drive the breaker
deterministically without ``time.sleep``-based latency.
"""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from typing import Awaitable, Callable, TypeVar

from llm_guardrail_proxy.proxy.exceptions import CircuitOpenError

T = TypeVar("T")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Asynchronous circuit breaker.

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures that trips the breaker.
    reset_seconds:
        Cooldown after which a single probe is permitted.
    clock:
        Monotonic-time source. Defaults to :func:`time.monotonic`; tests
        inject a callable that returns a controllable virtual clock.
    """

    __slots__ = (
        "_clock",
        "_failure_threshold",
        "_failures",
        "_lock",
        "_opened_at",
        "_reset_seconds",
        "_state",
    )

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_seconds <= 0:
            raise ValueError("reset_seconds must be > 0")

        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._state: BreakerState = BreakerState.CLOSED
        self._failures: int = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        return self._state

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn`` through the breaker.

        Raises
        ------
        CircuitOpenError
            When the breaker is open and the cooldown has not yet elapsed.
        Exception
            Any exception raised by ``fn`` is re-raised after recording
            the failure.
        """

        await self._before_call()
        try:
            result = await fn()
        except Exception:
            await self._record_failure()
            raise
        await self._record_success()
        return result

    # ----------------------------------------------------------- internals

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state is BreakerState.OPEN:
                assert self._opened_at is not None
                elapsed = self._clock() - self._opened_at
                if elapsed < self._reset_seconds:
                    raise CircuitOpenError(
                        "Upstream circuit breaker is open; refusing call."
                    )
                # Cooldown elapsed — promote to half-open for a single probe.
                self._state = BreakerState.HALF_OPEN

    async def _record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None
            self._state = BreakerState.CLOSED

    async def _record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = self._clock()
