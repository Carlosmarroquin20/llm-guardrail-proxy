"""Unit tests for provider adapters.

The adapters are the only place where wire-format quirks live, so the suite
exhaustively exercises each shape variant the public providers document.
"""

from __future__ import annotations

import json

import pytest

from llm_guardrail_proxy.proxy.envelope import ParsedPrompt, Provider
from llm_guardrail_proxy.proxy.exceptions import (
    PromptExtractionError,
    ProviderResolutionError,
)
from llm_guardrail_proxy.proxy.providers import (
    AnthropicMessagesAdapter,
    OpenAIChatAdapter,
    resolve_adapter,
    supported_paths,
)


def _b(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


# ----------------------------------------------------------- dispatch table


class TestResolveAdapter:
    def test_known_openai_path(self) -> None:
        adapter = resolve_adapter("/v1/chat/completions")
        assert adapter.provider is Provider.OPENAI

    def test_known_anthropic_path(self) -> None:
        adapter = resolve_adapter("/v1/messages")
        assert adapter.provider is Provider.ANTHROPIC

    def test_trailing_slash_is_tolerated(self) -> None:
        adapter = resolve_adapter("/v1/chat/completions/")
        assert adapter.provider is Provider.OPENAI

    def test_unknown_path_raises(self) -> None:
        with pytest.raises(ProviderResolutionError):
            resolve_adapter("/v1/audio/transcriptions")

    def test_supported_paths_matches_registry(self) -> None:
        paths = supported_paths()
        assert "/v1/chat/completions" in paths
        assert "/v1/messages" in paths


# ---------------------------------------------------------------- OpenAI


class TestOpenAIChatAdapter:
    adapter = OpenAIChatAdapter()

    def test_simple_message_is_extracted(self) -> None:
        body = _b(
            {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        result = self.adapter.parse(body)
        assert result == ParsedPrompt(
            provider=Provider.OPENAI, model="gpt-4o", content="hello"
        )

    def test_multipart_content_is_flattened(self) -> None:
        body = _b(
            {
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "alpha"},
                            {"type": "text", "text": "beta"},
                            {"type": "image_url", "image_url": {"url": "..."}},
                        ],
                    }
                ],
            }
        )
        result = self.adapter.parse(body)
        assert result.content == "alpha\nbeta"

    def test_multiple_messages_are_joined(self) -> None:
        body = _b(
            {
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "hi"},
                ],
            }
        )
        result = self.adapter.parse(body)
        assert result.content == "be brief\nhi"

    @pytest.mark.parametrize(
        "payload",
        [
            {},  # missing everything
            {"model": "gpt-4o"},  # missing messages
            {"model": "gpt-4o", "messages": []},  # empty messages
            {"messages": [{"role": "user", "content": "hi"}]},  # missing model
            {"model": "", "messages": [{"role": "user", "content": "hi"}]},  # empty model
        ],
    )
    def test_invalid_bodies_raise(self, payload: dict) -> None:
        with pytest.raises(PromptExtractionError):
            self.adapter.parse(_b(payload))

    def test_empty_bytes_raise(self) -> None:
        with pytest.raises(PromptExtractionError):
            self.adapter.parse(b"")

    def test_non_json_raises(self) -> None:
        with pytest.raises(PromptExtractionError):
            self.adapter.parse(b"not json")

    def test_top_level_array_is_rejected(self) -> None:
        with pytest.raises(PromptExtractionError):
            self.adapter.parse(b"[]")


# --------------------------------------------------------------- Anthropic


class TestAnthropicMessagesAdapter:
    adapter = AnthropicMessagesAdapter()

    def test_system_and_user_are_concatenated(self) -> None:
        body = _b(
            {
                "model": "claude-3-5-sonnet-latest",
                "system": "be concise",
                "messages": [{"role": "user", "content": "summarise"}],
            }
        )
        result = self.adapter.parse(body)
        assert result.provider is Provider.ANTHROPIC
        assert result.content == "be concise\nsummarise"

    def test_missing_system_is_tolerated(self) -> None:
        body = _b(
            {
                "model": "claude-3-5-sonnet-latest",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        result = self.adapter.parse(body)
        assert result.content == "hi"

    def test_structured_system_is_flattened(self) -> None:
        body = _b(
            {
                "model": "claude-3-5-sonnet-latest",
                "system": [
                    {"type": "text", "text": "context-1"},
                    {"type": "text", "text": "context-2"},
                ],
                "messages": [{"role": "user", "content": "go"}],
            }
        )
        result = self.adapter.parse(body)
        assert "context-1" in result.content
        assert "context-2" in result.content
        assert "go" in result.content

    def test_missing_model_raises(self) -> None:
        with pytest.raises(PromptExtractionError):
            self.adapter.parse(
                _b({"messages": [{"role": "user", "content": "hi"}]})
            )
