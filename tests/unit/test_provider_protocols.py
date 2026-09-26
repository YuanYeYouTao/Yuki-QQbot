"""Wire-level parity and private checkpoint recovery, without paid calls."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.conftest import build_harness, make_settings

from qq_ai_bot.deployment_setup.service import build_model_profiles
from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatTool,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolType,
    ProviderContinuation,
    ReasoningEffort,
)
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.base import (
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMUnsupportedFeatureError,
)
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.vendor_policy import CHAT_VENDORS, ChatWireOptions
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog, load_model_profile_catalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_journal import decode_transcript, encode_transcript
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.turn_transcript import TurnTranscript


def request():
    return ChatRequest(
        messages=(ChatMessage("system", "fixed"), ChatMessage("user", "task")),
        model="thinking-model",
        thinking_enabled=True,
        reasoning_effort=ReasoningEffort.LOW,
        max_output_tokens=8192,
        temperature=0.7,
        tools=(ChatTool("inspect", "Read evidence", {"type": "object", "properties": {}}),),
        tool_choice="auto",
    )


def provider(kind, client, **kwargs):
    return kind(
        base_url="https://wire.invalid/v1",
        api_key="synthetic-key",
        timeout_seconds=1,
        max_retries=0,
        client=client,
        **kwargs,
    )


@pytest.mark.parametrize("vendor", sorted(CHAT_VENDORS))
async def test_vendor_client_and_named_tool_contract(vendor):
    profile = ModelProfile(
        id="main",
        provider=vendor,
        protocol=ModelProtocol.CHAT_COMPLETIONS,
        base_url="https://wire.invalid/v1",
        api_key_env="SYNTHETIC_KEY",
        model="thinking-model",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=8192,
        capabilities={ModelCapability.REASONING, ModelCapability.TOOLS},
    )
    pool = ModelClientPool(secret_overrides={"SYNTHETIC_KEY": "synthetic"})
    try:
        adapter = pool.get(profile)
        assert isinstance(adapter, OpenAICompatibleProvider)
        payload = adapter._build_payload(replace(request(), tool_choice="inspect"))
        assert payload["tools"][0]["function"]["name"] == "inspect"
        assert "temperature" not in payload
        if vendor == "deepseek":
            assert "tool_choice" not in payload
        else:
            assert payload["tool_choice"] == {"type": "function", "function": {"name": "inspect"}}
        if vendor in {"openai", "azure_openai"}:
            assert payload["max_completion_tokens"] == 8192 and "thinking" not in payload
        elif vendor == "qwen":
            assert payload["enable_thinking"] is True and "reasoning_effort" not in payload
        elif vendor in {"deepseek", "moonshot", "doubao", "zhipu"}:
            assert payload["thinking"] == {"type": "enabled"}
        elif vendor == "minimax":
            assert payload["reasoning_split"] is True and "reasoning_effort" not in payload
        elif vendor == "openrouter":
            assert payload["reasoning"]["effort"] == "low"
    finally:
        await pool.close()


@pytest.mark.parametrize(
    "kind", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
async def test_signed_tool_result_and_redirect_survive_journal(kind):
    wires = []

    def transport(req):
        wires.append(json.loads(req.content))
        if kind is AnthropicMessagesProvider:
            body = {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "signed"},
                    {"type": "tool_use", "id": "call-1", "name": "inspect", "input": {}},
                ],
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                    "output_tokens": 4,
                },
            }
        elif kind is GeminiProvider:
            body = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {"name": "inspect", "args": {}},
                                    "thoughtSignature": "signed",
                                }
                            ],
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 15,
                    "candidatesTokenCount": 4,
                    "thoughtsTokenCount": 2,
                    "totalTokenCount": 21,
                },
            }
        else:
            body = {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_details": [
                                {"type": "reasoning.encrypted", "data": "signed"}
                            ],
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "inspect",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider(kind, client)
        original = request()
        answer = await adapter.complete(original)
        assert answer.continuation is not None
        transcript = TurnTranscript(original.messages)
        transcript.accept(answer.continuation)
        transcript.append_result(answer.tool_calls[0].id, '{"ok": true}')
        transcript.append(ChatMessage("user", "redirect after receipt"))
        encoded = json.loads(json.dumps(encode_transcript(transcript)))
        restored = decode_transcript(encoded).request()
        assert restored == transcript.request()
        await adapter.complete(
            replace(
                original,
                messages=restored.messages,
                continuation=restored.continuation,
                continuation_items=restored.items,
            )
        )
        sequence_key = "contents" if kind is GeminiProvider else "messages"
        assert wires[1][sequence_key][: len(wires[0][sequence_key])] == wires[0][sequence_key]
        tail_text = json.dumps(wires[1][sequence_key], ensure_ascii=False)
        assert tail_text.index('"signed"') < tail_text.index("ok") < tail_text.index("redirect")
        assert "_call_ids" not in tail_text
        assert wires[1]["tools"] == wires[0]["tools"]
        if kind is AnthropicMessagesProvider:
            assert answer.prompt_tokens == 15 and answer.total_tokens == 19
        elif kind is GeminiProvider:
            assert answer.completion_tokens == 6 and answer.reasoning_tokens == 2


@pytest.mark.parametrize(
    "kind", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
async def test_image_and_json_schema_reach_wire(kind):
    async with httpx.AsyncClient() as client:
        adapter = provider(kind, client)
        original = replace(
            request(),
            tools=(),
            messages=(
                ChatMessage(
                    "user",
                    "image",
                    images=(ChatImage("data:image/png;base64,aW1hZ2U="),),
                ),
            ),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "strict": True,
                    "schema": {"type": "object"},
                },
            },
        )
        payload = adapter._build_payload(original)
        assert "aW1hZ2U=" in json.dumps(payload)
        assert "schema" in json.dumps(payload).lower()
        with pytest.raises(LLMInvalidRequestError):
            adapter._build_payload(
                replace(original, messages=(replace(original.messages[0], role="assistant"),))
            )


@pytest.mark.parametrize("reason", ["length", "stop"])
async def test_chat_reasoning_citations_and_usage(reason):
    def transport(req):
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "choices": [
                    {
                        "finish_reason": reason,
                        "message": {
                            "content": [{"type": "text", "text": "answer"}],
                            "reasoning_content": "private",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://example.com/source",
                                        "title": "Source",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        result = await provider(OpenAICompatibleProvider, client).complete(request())
        assert result.content == "answer" and result.reasoning_content == "private"
        assert result.citations[0].url == "https://example.com/source"
        assert result.cached_prompt_tokens == 4 and result.reasoning_tokens == 3
        assert result.status is (
            ModelResponseStatus.INCOMPLETE if reason == "length" else ModelResponseStatus.COMPLETED
        )


async def test_chat_never_ignores_unsupported_native_or_foreign_state():
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client)
        native = (NativeToolDefinition(NativeToolType.WEB_SEARCH),)
        with pytest.raises(LLMUnsupportedFeatureError):
            adapter._build_payload(replace(request(), native_tools=native))
        with pytest.raises(LLMInvalidRequestError):
            adapter._build_payload(
                replace(
                    request(),
                    continuation=ProviderContinuation(
                        "openai",
                        "responses",
                        (),
                    ),
                )
            )
        adapter = provider(
            OpenAICompatibleProvider,
            client,
            provider_name="openai",
            options=ChatWireOptions(native_web_search=True),
        )
        payload = adapter._build_payload(replace(request(), tools=(), native_tools=native))
        assert payload["web_search_options"] == {} and payload["max_completion_tokens"] == 8192
        with pytest.raises(LLMUnsupportedFeatureError):
            adapter._build_payload(replace(request(), native_tools=native))


@pytest.mark.parametrize(
    "message",
    [
        {"content": "text", "tool_calls": [{"function": {"name": "inspect", "arguments": "{}"}}]},
        {
            "content": "text",
            "tool_calls": [{"id": "same", "function": {"name": "x", "arguments": "{}"}}] * 2,
        },
    ],
)
async def test_chat_rejects_malformed_or_duplicate_calls(message):
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client)
        with pytest.raises(LLMInvalidResponseError):
            adapter._parse(httpx.Response(200, json={"choices": [{"message": message}]}), request())


async def test_mistral_thinking_chunks_are_private_and_replay_losslessly():
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client, provider_name="mistral")
        original = request()
        assert adapter._build_payload(original)["reasoning_effort"] == "high"
        chunks = [
            {"type": "thinking", "thinking": [{"type": "text", "text": "private"}]},
            {"type": "text", "text": "answer"},
        ]
        answer = adapter._parse(
            httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": chunks}}]}
            ),
            original,
        )
        assert answer.content == "answer" and answer.reasoning_content == "private"
        payload = adapter._build_payload(replace(original, continuation=answer.continuation))
        assert payload["messages"][-1]["content"] == chunks
        with pytest.raises(LLMUnsupportedFeatureError):
            adapter._build_payload(replace(original, reasoning_effort=ReasoningEffort.MAX))


@pytest.mark.parametrize(
    "protocol,vendor",
    [
        ("chat_completions", "qwen"),
        ("responses", "openai"),
        ("anthropic_messages", "anthropic"),
        ("gemini", "gemini"),
    ],
)
def test_setup_generates_valid_vendor_catalog(tmp_path, protocol, vendor):
    path = tmp_path / "profiles.toml"
    path.write_text(
        build_model_profiles(main_protocol=protocol, main_provider=vendor, flash_enabled=False),
        encoding="utf-8",
    )
    catalog = load_model_profile_catalog(
        path,
        legacy_provider="fake",
        legacy_base_url="",
        legacy_model="fake",
        legacy_timeout_seconds=1,
        legacy_max_retries=0,
        legacy_temperature=0,
        legacy_max_output_tokens=1,
        legacy_thinking_enabled=True,
        environment={"LLM_BASE_URL": "https://wire.invalid/v1", "LLM_MODEL": "thinking-model"},
    )
    main = catalog.profiles[
        catalog.routes[
            next(task for task in catalog.routes if task.value == "chat_agent")
        ].profile_id
    ]
    assert main.provider == vendor and main.protocol.value == protocol
    if protocol == "responses":
        assert catalog.profiles["self_reflection"].provider == vendor


def test_multi_vendor_example_loads_without_reading_secrets():
    catalog = load_model_profile_catalog(
        Path("config/model_profiles.providers.example.toml"),
        legacy_provider="fake",
        legacy_base_url="",
        legacy_model="fake",
        legacy_timeout_seconds=1,
        legacy_max_retries=0,
        legacy_temperature=0,
        legacy_max_output_tokens=1,
        legacy_thinking_enabled=True,
        environment={
            name: "https://wire.invalid/v1" if name.endswith("BASE_URL") else "thinking-model"
            for name in (
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_MODEL",
                "GEMINI_BASE_URL",
                "GEMINI_MODEL",
                "LLM_FLASH_BASE_URL",
                "LLM_FLASH_MODEL",
            )
        },
    )
    assert {profile.provider for profile in catalog.profiles.values()} == {
        "anthropic",
        "qwen",
        "gemini",
    }
    assert len({profile.api_key_env for profile in catalog.profiles.values()}) == 3


@pytest.mark.parametrize(
    "kind", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
@pytest.mark.parametrize("empty", [False, True])
async def test_truncated_tool_call_recovers_without_executing(database, kind, empty):
    wires = []

    def transport(req):
        wires.append(json.loads(req.content))
        first = len(wires) == 1
        if kind is AnthropicMessagesProvider:
            body = {
                "stop_reason": "max_tokens" if first else "end_turn",
                "content": [
                    {"type": "tool_use", "id": "unfinished", "name": "inspect", "input": {}}
                    if first
                    else {"type": "text", "text": "done"}
                ],
            }
        elif kind is GeminiProvider:
            body = {
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS" if first else "STOP",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "inspect",
                                        "args": {},
                                        "id": "unfinished",
                                    },
                                    "thoughtSignature": "signed",
                                }
                                if first
                                else {"text": "done"}
                            ],
                        },
                    }
                ]
            }
        else:
            body = {
                "choices": [
                    {
                        "finish_reason": "length" if first else "stop",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "unfinished",
                                    "type": "function",
                                    "function": {"name": "inspect", "arguments": "{}"},
                                }
                            ],
                        }
                        if first
                        else {"content": "done"},
                    }
                ]
            }
        if first and empty:
            if kind is AnthropicMessagesProvider:
                body["content"] = []
            elif kind is GeminiProvider:
                body["candidates"][0]["content"] = {"role": "model"}
            else:
                body["choices"][0]["message"] = {"content": None}
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        harness = build_harness(database, make_settings(database.url), provider(kind, client))
        chat = harness.processor._chat
        tools = request().tools
        execute = AsyncMock(side_effect=AssertionError("truncated calls must not execute"))
        backend = SimpleNamespace(
            definitions=lambda *args, **kwargs: tools,
            execute=execute,
            finalize=lambda text, runtime: text,
        )
        runtime = AgentRuntime(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id="10001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="truncated",
            current_group_id=None,
            bot_user_id="80001",
            gateway=None,
            runtime_config=await chat._runtime_config.snapshot(),
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset(),
            max_tool_calls=2,
            max_model_requests=2,
            fixed_tools=tools,
        )
        result = await chat._agent_runner.run(
            (ChatMessage("system", "fixed"), ChatMessage("user", "inspect")), runtime, backend
        )
        assert result.text == "done" and result.model_requests == 2
        execute.assert_not_called()
    assert wires[0]["tools"] == wires[1]["tools"]
    if not empty:
        assert "provider_response_incomplete" in json.dumps(wires[1])
        assert "unfinished" in json.dumps(wires[1])


async def test_deepseek_chat_dsml_uses_only_declared_tools():
    markup = (
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="inspect">'
        '<｜｜DSML｜｜parameter name="query" string="true">evidence</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>"
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(OpenAICompatibleProvider, client, provider_name="deepseek")
        response = httpx.Response(
            200,
            json={
                "id": "stable-response",
                "choices": [{"finish_reason": "stop", "message": {"content": markup}}],
            },
        )
        result = adapter._parse(response, request())
        assert result.content == "" and result.tool_calls[0].function.name == "inspect"
        assert json.loads(result.tool_calls[0].function.arguments) == {"query": "evidence"}
        assert adapter._parse(response, request()).tool_calls == result.tool_calls
        with pytest.raises(LLMInvalidResponseError):
            adapter._parse(response, replace(request(), tools=()))


def test_existing_responses_revision_ignores_empty_new_defaults():
    profile = ModelProfile(
        id="main",
        provider="deepseek",
        protocol=ModelProtocol.RESPONSES,
        base_url="https://wire.invalid",
        api_key_env="UNUSED",
        model="thinking-model",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=8192,
        capabilities={ModelCapability.REASONING, ModelCapability.TOOLS},
    )
    routes = {task: ModelRoute(task=task, profile_id="main") for task in ModelTask}
    executor = TaskModelExecutor(
        router=ModelRouter(ModelProfileCatalog(profiles={"main": profile}, routes=routes)),
        pool=ModelClientPool(),
    )
    legacy = {
        "route": routes[ModelTask.CHAT_AGENT].model_dump(mode="json"),
        "profile": profile.model_dump(
            mode="json", exclude={"wire_options", "headers", "max_output_tokens_limit"}
        ),
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
    assert executor.profile_revision(ModelTask.CHAT_AGENT) == expected
    changed = profile.model_copy(update={"headers": {"x-custom-feature": "enabled"}})
    alternate = TaskModelExecutor(
        router=ModelRouter(ModelProfileCatalog(profiles={"main": changed}, routes=routes)),
        pool=ModelClientPool(),
    )
    assert alternate.profile_revision(ModelTask.CHAT_AGENT) != expected


async def test_gemini_parallel_receipts_keep_signature_and_call_order():
    async with httpx.AsyncClient() as client:
        adapter = provider(GeminiProvider, client)
        original = request()
        parts = [
            {
                "functionCall": {"name": "inspect", "args": {"index": index}},
                **({"thoughtSignature": "signed"} if index == 1 else {}),
            }
            for index in (1, 2)
        ]
        answer = adapter._parse(
            httpx.Response(
                200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": parts}}]}
            ),
            original,
        )
        transcript = TurnTranscript(original.messages)
        transcript.accept(answer.continuation)
        for call in answer.tool_calls:
            transcript.append_result(call.id, call.function.arguments)
        sequence = transcript.request()
        payload = adapter._build_payload(
            replace(original, continuation=sequence.continuation, continuation_items=sequence.items)
        )
        assert payload["contents"][-2]["parts"] == parts
        receipts = payload["contents"][-1]["parts"]
        assert [
            json.loads(part["functionResponse"]["response"]["output"])["index"] for part in receipts
        ] == [1, 2]
        assert all("_call_ids" not in item for item in payload["contents"])
