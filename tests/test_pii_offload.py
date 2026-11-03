"""The PII middleware must not block the event loop while spaCy runs.

Uses a stand-in scanner so the test does not need Presidio installed.
"""

from __future__ import annotations

import time

import anyio

from llm_guardrail_proxy.proxy.envelope import (
    Continue,
    ParsedPrompt,
    Provider,
    ProxyRequest,
)
from llm_guardrail_proxy.proxy.middlewares.pii_scan import PiiScanMiddleware


class _SlowScanner:
    def scan(self, text: str) -> tuple[()]:
        time.sleep(0.3)  # blocking, like spaCy NER on a long prompt
        return ()


def _request() -> ProxyRequest:
    return ProxyRequest(
        path="/v1/chat/completions",
        method="POST",
        headers={},
        raw_body=b"{}",
        parsed=ParsedPrompt(provider=Provider.OPENAI, model="gpt-4o", content="hello"),
    )


async def test_slow_pii_scan_does_not_block_the_event_loop() -> None:
    middleware = PiiScanMiddleware(scanner=_SlowScanner())  # type: ignore[arg-type]
    ticks = 0
    outcome = None

    async def ticker() -> None:
        nonlocal ticks
        while outcome is None:
            ticks += 1
            await anyio.sleep(0.01)

    async def scan() -> None:
        nonlocal outcome
        outcome = await middleware.process(_request())

    async with anyio.create_task_group() as tg:
        tg.start_soon(ticker)
        tg.start_soon(scan)

    assert isinstance(outcome, Continue)
    # With the scan on the event loop the ticker would be frozen for the full
    # 300 ms and tick about once; offloaded, it keeps running throughout.
    assert ticks >= 10
