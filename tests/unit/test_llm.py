"""OpenAI-compatible provider retry and response validation tests."""

from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatTool, ReasoningEffort
from qq_ai_bot.llm.base import LLMEmptyResponseError, LLMTimeoutError
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider


def request() -> ChatRequest:
    return ChatRequest(
        messages=(ChatMessage("user", "hello"),),
        model="test-model",
        temperature=0,
        max_output_tokens=10,
    )


@pytest.mark.asyncio
async def test_timeout_is_retried_once_then_sanitized() -> None:
    attempts = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("synthetic", request=http_request)

    async with httpx.AsyncClient(
        base_url="https://provider.test/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://provider.test/v1",
            api_key="secret",
            timeout_seconds=0.1,
            max_retries=1,
            client=client,
        )
        with pytest.raises(LLMTimeoutError):
            await provider.complete(request())
    assert attempts == 2


@pytest.mark.asyncio
async def test_5xx_is_retried_and_then_succeeds() -> None:
    attempts = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=http_request)
        return httpx.Response(
            200,
            request=http_request,
            json={"id": "request-1", "choices": [{"message": {"content": "answer"}}]},
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://provider.test/v1",
            api_key="secret",
            timeout_seconds=1,
            max_retries=1,
            client=client,
        )
        response = await provider.complete(request())
    assert attempts == 2
    assert response.content == "answer"


@pytest.mark.asyncio
async def test_read_error_is_retried_and_then_succeeds() -> None:
    attempts = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("synthetic", request=http_request)
        return httpx.Response(
            200,
            request=http_request,
            json={"id": "request-1", "choices": [{"message": {"content": "answer"}}]},
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://provider.test/v1",
            api_key="secret",
            timeout_seconds=1,
            max_retries=1,
            client=client,
        )
        response = await provider.complete(request())

    assert attempts == 2
    assert response.content == "answer"


@pytest.mark.asyncio
async def test_empty_provider_response_is_rejected() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=http_request,
            json={"choices": [{"message": {"content": " "}}]},
        )

    async with httpx.AsyncClient(
        base_url="https://provider.test/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://provider.test/v1",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        with pytest.raises(LLMEmptyResponseError):
            await provider.complete(request())


@pytest.mark.asyncio
async def test_optional_thinking_mode_is_sent_when_configured() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        assert payload["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            request=http_request,
            json={"choices": [{"message": {"content": "answer"}}]},
        )

    chat_request = request()
    chat_request = ChatRequest(
        messages=chat_request.messages,
        model=chat_request.model,
        temperature=chat_request.temperature,
        max_output_tokens=chat_request.max_output_tokens,
        thinking_enabled=False,
    )
    async with httpx.AsyncClient(
        base_url="https://provider.test/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://provider.test/v1",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        response = await provider.complete(chat_request)
    assert response.content == "answer"


@pytest.mark.asyncio
async def test_deepseek_max_reasoning_omits_tool_choice_in_thinking_mode() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "max"
        assert "tools" in payload
        assert "tool_choice" not in payload
        return httpx.Response(
            200,
            request=http_request,
            json={"choices": [{"message": {"content": "answer"}}]},
        )

    chat_request = ChatRequest(
        messages=(ChatMessage("user", "hello"),),
        model="deepseek-v4-flash",
        temperature=0,
        max_output_tokens=10,
        thinking_enabled=True,
        reasoning_effort=ReasoningEffort.MAX,
        tools=(ChatTool(name="search", description="search", parameters={}),),
        tool_choice="auto",
    )
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        response = await provider.complete(chat_request)
    assert response.content == "answer"


@pytest.mark.asyncio
async def test_deepseek_non_thinking_also_omits_tool_choice() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        assert payload["thinking"] == {"type": "disabled"}
        assert "tools" in payload
        assert "tool_choice" not in payload
        return httpx.Response(
            200,
            request=http_request,
            json={"choices": [{"message": {"content": "answer"}}]},
        )

    chat_request = ChatRequest(
        messages=(ChatMessage("user", "hello"),),
        model="deepseek-v4-flash",
        thinking_enabled=False,
        tools=(ChatTool(name="search", description="search", parameters={}),),
        tool_choice="auto",
    )
    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        response = await provider.complete(chat_request)
    assert response.content == "answer"


@pytest.mark.asyncio
async def test_non_deepseek_thinking_keeps_explicit_tool_choice() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        assert payload["tool_choice"] == "auto"
        return httpx.Response(
            200,
            request=http_request,
            json={"choices": [{"message": {"content": "answer"}}]},
        )

    chat_request = ChatRequest(
        messages=(ChatMessage("user", "hello"),),
        model="another-reasoning-model",
        thinking_enabled=True,
        tools=(ChatTool(name="search", description="search", parameters={}),),
        tool_choice="auto",
    )
    async with httpx.AsyncClient(
        base_url="https://provider.test/v1", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://provider.test/v1",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        response = await provider.complete(chat_request)
    assert response.content == "answer"
