"""Minimal OpenAI / Anthropic stand-in for benchmarking.

Returns a fixed, small completion immediately, so any latency measured
through the proxy is the proxy's own work plus loopback networking — not
model time.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

app = FastAPI()

_CHAT_REPLY: dict[str, Any] = {
    "id": "chatcmpl-bench",
    "object": "chat.completion",
    "model": "gpt-4o",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}

_MESSAGES_REPLY: dict[str, Any] = {
    "id": "msg_bench",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions() -> dict[str, Any]:
    return _CHAT_REPLY


@app.post("/v1/messages")
async def messages() -> dict[str, Any]:
    return _MESSAGES_REPLY
