"""Tests for the ``redact`` method on provider adapters."""

from __future__ import annotations

import json

from llm_guardrail_proxy.proxy.providers import (
    AnthropicMessagesAdapter,
    OpenAIChatAdapter,
)


def _b(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class TestOpenAIRedact:
    adapter = OpenAIChatAdapter()

    def test_simple_string_content_is_rewritten(self) -> None:
        body = _b(
            {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "email me at a@b.com"}],
            }
        )
        result = json.loads(self.adapter.redact(body, [("a@b.com", "[REDACTED]")]))
        assert result["messages"][0]["content"] == "email me at [REDACTED]"

    def test_multipart_text_parts_are_rewritten(self) -> None:
        body = _b(
            {
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "leaked a@b.com"},
                            {"type": "image_url", "image_url": {"url": "..."}},
                        ],
                    }
                ],
            }
        )
        result = json.loads(self.adapter.redact(body, [("a@b.com", "[X]")]))
        parts = result["messages"][0]["content"]
        assert parts[0]["text"] == "leaked [X]"
        # Image parts are passed through unchanged — redaction does not
        # touch non-text modalities in Phase 3b.
        assert parts[1]["type"] == "image_url"

    def test_other_top_level_fields_are_preserved(self) -> None:
        body = _b(
            {
                "model": "gpt-4o",
                "temperature": 0.7,
                "messages": [{"role": "user", "content": "leak"}],
            }
        )
        result = json.loads(self.adapter.redact(body, [("leak", "ok")]))
        assert result["model"] == "gpt-4o"
        assert result["temperature"] == 0.7

    def test_every_occurrence_is_replaced(self) -> None:
        body = _b(
            {
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": "secret here"},
                    {"role": "user", "content": "secret again, secret again"},
                ],
            }
        )
        result = json.loads(self.adapter.redact(body, [("secret", "X")]))
        assert result["messages"][0]["content"] == "X here"
        assert result["messages"][1]["content"] == "X again, X again"


class TestAnthropicRedact:
    adapter = AnthropicMessagesAdapter()

    def test_system_string_is_redacted(self) -> None:
        body = _b(
            {
                "model": "claude-3-5-sonnet-latest",
                "system": "context contains a@b.com",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        result = json.loads(self.adapter.redact(body, [("a@b.com", "[X]")]))
        assert result["system"] == "context contains [X]"

    def test_system_list_is_redacted(self) -> None:
        body = _b(
            {
                "model": "claude-3-5-sonnet-latest",
                "system": [
                    {"type": "text", "text": "alpha a@b.com"},
                    {"type": "text", "text": "beta"},
                ],
                "messages": [{"role": "user", "content": "go"}],
            }
        )
        result = json.loads(self.adapter.redact(body, [("a@b.com", "[X]")]))
        assert result["system"][0]["text"] == "alpha [X]"
        assert result["system"][1]["text"] == "beta"

    def test_user_message_is_redacted(self) -> None:
        body = _b(
            {
                "model": "claude-3-5-sonnet-latest",
                "messages": [{"role": "user", "content": "leak a@b.com"}],
            }
        )
        result = json.loads(self.adapter.redact(body, [("a@b.com", "[X]")]))
        assert result["messages"][0]["content"] == "leak [X]"
