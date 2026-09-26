"""Provider text channels cannot implicitly become public chat messages."""

import json
from itertools import pairwise

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, InboundMessage, SenderIdentity
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState

_PLANNING = "I need to decide how to answer before speaking."
_REASONING = "Synthetic hidden reasoning; never a public message."
_ANSWER = "我在，刚才检查好了。"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", list(ModelProtocol))
@pytest.mark.parametrize("explicit_send", [False, True])
async def test_provider_text_requires_explicit_delivery(
    database, tmp_path, protocol, explicit_send
):
    """Replay #55's output_text contamination without calling a live provider.

    Use the real adapter, runner, send tool, fake gateway and SQLite ledger.
    The boundary is structural: no keyword classifier is expected to recognize
    the synthetic planning text or rewrite the already committed transcript.
    """
    env = await social_env(database, tmp_path)
    requests = []
    outputs = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        index = len(requests)
        should_send = explicit_send and index == 2
        arguments = json.dumps({"text": _ANSWER}, ensure_ascii=False)
        if protocol is ModelProtocol.RESPONSES:
            output = [
                {
                    "id": f"reason-{index}",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": _REASONING}],
                },
                {
                    "id": f"message-{index}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": _PLANNING}],
                },
            ]
            if should_send:
                output.append(
                    {
                        "id": "send-item",
                        "type": "function_call",
                        "call_id": "send-answer",
                        "name": "send_message",
                        "arguments": arguments,
                        "status": "completed",
                    }
                )
            outputs.append(output)
            body = {"id": f"response-{index}", "status": "completed", "output": output}
        elif protocol is ModelProtocol.ANTHROPIC_MESSAGES:
            blocks = [
                {"type": "thinking", "thinking": _REASONING, "signature": "signed-state"},
                {"type": "text", "text": _PLANNING},
            ]
            if should_send:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": "send-answer",
                        "name": "send_message",
                        "input": {"text": _ANSWER},
                    }
                )
            outputs.append({"role": "assistant", "content": blocks})
            body = {"content": blocks, "stop_reason": "tool_use" if should_send else "end_turn"}
        elif protocol is ModelProtocol.GEMINI:
            parts = [
                {"text": _REASONING, "thought": True, "thoughtSignature": "signed-state"},
                {"text": _PLANNING},
            ]
            if should_send:
                parts.append(
                    {
                        "functionCall": {
                            "id": "send-answer",
                            "name": "send_message",
                            "args": {"text": _ANSWER},
                        },
                        "thoughtSignature": "tool-signature",
                    }
                )
            model_content = {"role": "model", "parts": parts}
            outputs.append(model_content)
            body = {"candidates": [{"content": model_content, "finishReason": "STOP"}]}
        else:
            message = {
                "role": "assistant",
                "content": _PLANNING,
                "reasoning_content": _REASONING,
            }
            if should_send:
                message["tool_calls"] = [
                    {
                        "id": "send-answer",
                        "type": "function",
                        "function": {"name": "send_message", "arguments": arguments},
                    }
                ]
            outputs.append(message)
            body = {
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if should_send else "stop"}
                ]
            }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider_type = {
            ModelProtocol.RESPONSES: DeepSeekResponsesProvider,
            ModelProtocol.CHAT_COMPLETIONS: OpenAICompatibleProvider,
            ModelProtocol.ANTHROPIC_MESSAGES: AnthropicMessagesProvider,
            ModelProtocol.GEMINI: GeminiProvider,
        }[protocol]
        provider = provider_type(
            base_url="https://wire.invalid",
            api_key="synthetic-key",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        harness = build_harness(
            database, make_settings(database.url, enabled_groups_csv="20001"), provider
        )
        chat = harness.processor._chat
        profile = ModelProfile(
            id="boundary",
            provider={
                ModelProtocol.RESPONSES: "deepseek",
                ModelProtocol.CHAT_COMPLETIONS: "openai_compatible",
                ModelProtocol.ANTHROPIC_MESSAGES: "anthropic",
                ModelProtocol.GEMINI: "gemini",
            }[protocol],
            protocol=protocol,
            base_url="https://wire.invalid",
            api_key_env="UNUSED_SYNTHETIC_KEY",
            model="synthetic-model",
            timeout_seconds=1,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=512,
            capabilities=frozenset(ModelCapability),
        )
        models = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={profile.id: profile},
                    routes={
                        task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask
                    },
                )
            ),
            pool=ModelClientPool(injected_profiles={profile.id: provider}),
        )
        chat._models = models
        chat._agent_runner._models = models
        chat._tools.social_service = env.service
        chat._agent_runner.main_contract = MainAgentContract(chat, ShortState(env.store))
        sender = MemorySender()
        result = await harness.processor.handle(
            InboundMessage(
                message_id="boundary-inbound",
                event_type="message:test",
                scope_type=ScopeType.GROUP,
                sender=SenderIdentity("10001"),
                text="检查好了吗？",
                bot_user_id="80001",
                group_id="20001",
                mentions_bot=True,
                conversation_id=env.context.conversation_id,
                legacy_conversation_key=ConversationScope.group("80001", "20001").key,
                person_id=env.person,
                space_id=env.space,
                presence_id=env.presence,
            ),
            sender,
        )

    assert len(requests) == (3 if explicit_send else 2)
    sequence_key = (
        "input"
        if protocol is ModelProtocol.RESPONSES
        else "contents"
        if protocol is ModelProtocol.GEMINI
        else "messages"
    )
    for previous, following in pairwise(requests):
        previous_input = previous[sequence_key]
        assert following[sequence_key][: len(previous_input)] == previous_input
        assert following["tools"] == previous["tools"]
        if protocol is ModelProtocol.RESPONSES:
            assert following["instructions"] == previous["instructions"]
            assert following["reasoning"] == previous["reasoning"] == {"effort": "low"}
    second_input = requests[1][sequence_key]
    offset = len(requests[0][sequence_key])
    if protocol is ModelProtocol.RESPONSES:
        assert second_input[offset : offset + len(outputs[0])] == outputs[0]
    else:
        assert second_input[offset] == outputs[0]
    assert "上一段最终正文没有发送给用户" in json.dumps(second_input, ensure_ascii=False)

    gateway_sends = [
        params
        for action, params in env.bot.calls
        if action in {"send_group_msg", "send_private_msg"}
    ]
    assert len(gateway_sends) == int(explicit_send)
    if explicit_send:
        assert result.reason == "chat" and result.sent_messages == 1
        assert gateway_sends[0]["message"] == [{"type": "text", "data": {"text": _ANSWER}}]
        assert not sender.messages
    else:
        # A bounded operational failure may be sent, never the provider's text.
        assert result.reason == "agent_output_failure"
        assert sender.messages
    async with database.sessions() as session:
        outbound_texts = list(
            await session.scalars(
                select(ChatEventModel.content).where(ChatEventModel.direction == "outbound")
            )
        )
    delivered = json.dumps(
        [gateway_sends, [message.text for message in sender.messages], outbound_texts],
        ensure_ascii=False,
    )
    assert _PLANNING not in delivered and _REASONING not in delivered
    assert outbound_texts.count(_ANSWER) == int(explicit_send)


@pytest.mark.asyncio
async def test_received_empty_response_preserves_reasoning_during_retry(database):
    # Current HTTP adapters reject completed empty responses before Runner receives
    # them. Exercise Runner's separate already-received ChatResponse contract;
    # transport exceptions must never fabricate an assistant message instead.
    responses = iter([ChatResponse("", 0, reasoning_content=_REASONING), ChatResponse("done", 0)])
    provider = FakeLLMProvider(lambda _: next(responses))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    runtime = AgentRuntime(
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="received-empty",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=2,
        max_model_requests=2,
    )
    result = await chat._agent_runner.run(
        (ChatMessage(role="user", content="report the result"),), runtime, None
    )
    assert result.text == "done" and result.model_requests == 2
    first, second = (request.messages for request in provider.requests)
    assert second[: len(first)] == first
    assert second[-2] == ChatMessage(role="assistant", content="", reasoning_content=_REASONING)
    assert second[-1].role == "system" and "上一响应正文为空" in second[-1].content
