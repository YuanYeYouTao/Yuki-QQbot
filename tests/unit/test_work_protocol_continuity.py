"""Explicit compaction task anchors and exact persisted provider request replay."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, update
from tests.conftest import build_harness, make_settings
from tests.support.runtime_wire import install_wire
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import JournalUnavailable, encode_transcript
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _control(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "anchor-test", {"trigger_event_id": 1}, validate)
    control.current = await repo.accept(
        lease, source_key="anchor-test", source={}, goal="retain the actual task"
    )
    return control


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_changed", [False, True])
async def test_compaction_keeps_explicit_task_after_restart(database, tmp_path, contract_changed):
    control = await _control(database, tmp_path)
    task = ChatMessage(
        "user",
        "original task plus its compiled runtime data",
        images=(ChatImage("data:image/png;base64,YXVkaXQ="),),
    )
    initial = (
        ChatMessage("system", "original fixed contract"),
        ChatMessage("user", "old rollup or historical user message"),
        ChatMessage("assistant", "old response"),
        task,
        ChatMessage("user", "transient work status"),
    )
    first = WorkSession(control, "original")
    transcript = await first.restore(TurnTranscript(initial), compaction_brief=task)
    await control.repository.checkpoint(
        control.lease, control.current["id"], None, models=3, tools=2
    )
    control.current = await control.repository.get(control.current["id"])
    control.known_effects = [{"run_id": "original-execution", "pending": True}]
    await first.save("paired")
    control.current = await control.repository.get(control.current["id"])
    fresh_task = ChatMessage("user", "new wakeup and refreshed runtime data")
    fresh_system = ChatMessage(
        "system", "new fixed contract" if contract_changed else initial[0].content
    )
    resumed = WorkSession(control, "new-contract" if contract_changed else "original")
    restored = await resumed.restore(
        TurnTranscript((fresh_system, fresh_task)), compaction_brief=fresh_task
    )
    if not contract_changed:
        assert restored.request() == transcript.request()
    else:
        carried = restored.request().messages[2]
        assert carried.content.endswith(task.content)
        assert "不代表当前权限" in carried.content
        assert carried.images == task.images
        assert restored.request().messages[:2] == (fresh_system, fresh_task)
    compacted = await resumed.compact("Completed checks, pending execution remains")
    assert compacted.chain_id != restored.chain_id
    assert compacted.request().messages[:2] == (fresh_system, task)
    assert len(compacted.request().messages) == 3
    summary = json.loads(compacted.request().messages[2].content)
    assert summary["execution_evidence"][0]["run_id"] == "original-execution"
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 3 and row["tool_calls"] == 2
    again = WorkSession(control, resumed.contract)
    await again.restore(TurnTranscript((fresh_task,)), compaction_brief=fresh_task)
    twice = await again.compact("A second bounded summary")
    assert twice.request().messages[:2] == (fresh_system, task)
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_type", [OpenAICompatibleProvider, AnthropicMessagesProvider, GeminiProvider]
)
async def test_native_checkpoint_replays_exact_http_after_sqlite_restart(
    database, tmp_path, provider_type
):
    control = await _control(database, tmp_path)
    task = ChatMessage("user", "original task")
    first = WorkSession(control, "unchanged-profile")
    transcript = await first.restore(
        TurnTranscript((ChatMessage("system", "fixed"), task)), compaction_brief=task
    )
    if provider_type is AnthropicMessagesProvider:
        tail = (
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "original-signature"},
                    {"type": "tool_use", "id": "original-call", "name": "read", "input": {}},
                ],
            },
        )
        answer = {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
    elif provider_type is GeminiProvider:
        tail = (
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "read", "args": {}},
                        "thoughtSignature": "original-signature",
                    }
                ],
                "_call_ids": ["original-call"],
            },
        )
        answer = {
            "candidates": [
                {"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": "done"}]}}
            ]
        }
    else:
        tail = (
            {
                "role": "assistant",
                "content": None,
                "reasoning_details": [
                    {"type": "reasoning.encrypted", "data": "original-signature"}
                ],
                "tool_calls": [
                    {
                        "id": "original-call",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
        )
        answer = {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}
    transcript.accept(
        ProviderContinuation(provider_type.provider_name, provider_type.protocol, tail)
    )
    transcript.append_result("original-call", '{"ok":true,"execution_id":"original-execution"}')
    transcript.append(ChatMessage("user", "redirect after receipt"))
    await control.repository.checkpoint(
        control.lease, control.current["id"], None, models=3, tools=1
    )
    control.current = await control.repository.get(control.current["id"])
    captured = []

    def transport(req):
        captured.append(req.content)
        return httpx.Response(200, json=answer)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        adapter = provider_type(
            base_url="https://wire.invalid/v1/",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )

        async def capture(value):
            sequence = value.request()
            await adapter.complete(
                ChatRequest(
                    messages=sequence.messages,
                    continuation=sequence.continuation,
                    continuation_items=sequence.items,
                    model="thinking-model",
                    tools=(ChatTool("read", "Read", {"type": "object"}),),
                    thinking_enabled=True,
                    max_output_tokens=8192,
                    request_chain_id=value.chain_id,
                )
            )

        await capture(transcript)
        await first.save("paired")
        restored = await WorkSession(control, "unchanged-profile").restore(
            TurnTranscript((ChatMessage("user", "new wakeup"),))
        )
        assert restored.chain_id == transcript.chain_id
        await capture(restored)
    assert captured[0] == captured[1]
    assert b"original-signature" in captured[1] and b"original-execution" in captured[1]
    assert b"new wakeup" not in captured[1]
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 3 and row["tool_calls"] == 1
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_changed", [False, True])
@pytest.mark.parametrize(
    "bad_anchor",
    [
        [],
        {"items": []},
        encode_transcript(TurnTranscript((ChatMessage("assistant", "not a task"),))),
        encode_transcript(TurnTranscript((ChatMessage("user", {"not": "text"}),))),
        {
            "items": [{"kind": "message", "value": {"role": ["user"]}}],
            "messages_count": 1,
            "chain_id": "bad",
            "continuation": None,
        },
    ],
)
async def test_corrupt_compaction_anchor_is_unavailable(
    database, tmp_path, contract_changed, bad_anchor
):
    control = await _control(database, tmp_path)
    task = ChatMessage("user", "task")
    first = WorkSession(control, "same")
    await first.restore(TurnTranscript((task,)), compaction_brief=task)
    await first.save("paired")
    async with database.sessions() as session, session.begin():
        raw = await session.scalar(
            select(journal.c.payload_json).where(journal.c.work_id == control.current["id"])
        )
        payload = json.loads(raw)
        payload["metadata"]["compaction_anchor"] = bad_anchor
        await session.execute(
            update(journal)
            .where(journal.c.work_id == control.current["id"])
            .values(payload_json=json.dumps(payload))
        )
    with pytest.raises(JournalUnavailable, match="compaction_anchor_corrupt"):
        await WorkSession(control, "changed" if contract_changed else "same").restore(
            TurnTranscript((task,)), compaction_brief=task
        )
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_journal_without_task_anchor_resumes_but_does_not_guess_one(database, tmp_path):
    control = await _control(database, tmp_path)
    first = WorkSession(control, "same")
    transcript = await first.restore(TurnTranscript((ChatMessage("user", "historical input"),)))
    await first.save("paired")
    resumed = WorkSession(control, "same")
    restored = await resumed.restore(
        TurnTranscript((ChatMessage("user", "fresh wakeup"),)),
        compaction_brief=ChatMessage("user", "fresh wakeup"),
    )
    assert restored.request() == transcript.request()
    with pytest.raises(JournalUnavailable, match="compaction_anchor_unavailable"):
        await resumed.compact("Summary cannot invent the original task")
    assert resumed.transcript is restored
    with pytest.raises(JournalUnavailable, match="compaction_anchor_unavailable"):
        await WorkSession(control, "changed-contract").restore(
            TurnTranscript((ChatMessage("user", "fresh wakeup"),)),
            compaction_brief=ChatMessage("user", "fresh wakeup"),
        )
    original = await WorkSession(control, "same").restore(TurnTranscript(()))
    assert original.request() == transcript.request()
    row = await control.repository.get(control.current["id"])
    assert row["model_requests"] == 0 and row["tool_calls"] == 0
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [DeepSeekResponsesProvider, OpenAIResponsesProvider])
async def test_responses_journal_replays_identical_http_bytes(database, tmp_path, provider_type):
    control = await _control(database, tmp_path)
    task = ChatMessage("user", "task", images=(ChatImage("data:image/png;base64,YXVkaXQ="),))
    first = WorkSession(control, "same")
    transcript = await first.restore(
        TurnTranscript((ChatMessage("system", "fixed"), task)), compaction_brief=task
    )
    transcript.accept(
        ProviderContinuation(
            provider_type.provider_name,
            "responses",
            (
                {
                    "id": "reason-1",
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "synthetic"}],
                },
                {
                    "id": "call-1",
                    "type": "function_call",
                    "call_id": "original-call",
                    "name": "read",
                    "arguments": "{}",
                    "status": "completed",
                },
            ),
        )
    )
    transcript.append_result("original-call", '{"ok":true}')
    captured = []

    def transport(request):
        captured.append(request.content)
        return httpx.Response(
            200,
            json={
                "id": "r-next",
                "status": "completed",
                "output": [
                    {
                        "id": "message-next",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://audit.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = provider_type(
            base_url="https://audit.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )

        async def capture(value):
            sequence = value.request()
            await provider.complete(
                ChatRequest(
                    messages=sequence.messages,
                    continuation=sequence.continuation,
                    continuation_items=sequence.items,
                    model="synthetic",
                    request_chain_id=value.chain_id,
                    tools=(
                        ChatTool(
                            "read",
                            "read",
                            {
                                "type": "object",
                                "properties": {"z": {"type": "string"}, "a": {"type": "integer"}},
                            },
                        ),
                    ),
                    tool_choice="auto",
                )
            )

        await capture(transcript)
        await first.save("paired")
        restored = await WorkSession(control, "same").restore(TurnTranscript(()))
        assert restored.chain_id == transcript.chain_id
        await capture(restored)
    assert captured[0] == captured[1]
    await control.repository.release(control.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "openai_responses"])
async def test_no_progress_recovery_keeps_tools_settings_and_local_execution_fence(
    database, protocol
):
    fixed = (ChatTool("read_probe", "Read audit data", {"type": "object"}),)
    executions = []
    provider = FakeLLMProvider(
        lambda _: ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(f"read-{len(provider.requests)}", ToolFunction("read_probe", "{}")),
            ),
        )
    )

    class Backend:
        def definitions(self, runtime, **kwargs):
            return fixed

        def begin_batch(self, *args):
            pass

        def parallel_safe(self, *args):
            return False

        def is_side_effecting(self, *args):
            return False

        async def execute(self, *args):
            executions.append("read")
            return '{"ok":true,"unchanged":true}'

        def finalize(self, text, runtime):
            return text

        def exhausted(self, runtime):
            raise AssertionError("unexpected exhaustion")

    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    client, wire = install_wire(
        chat,
        provider,
        "responses" if protocol == "openai_responses" else protocol,
        native=protocol == "openai_responses",
    )
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="no-progress",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        fixed_tools=fixed,
    )
    try:
        result = await chat._agent_runner.run(
            (ChatMessage("system", "fixed"), ChatMessage("user", "audit")), runtime, Backend()
        )
    finally:
        await client.aclose()
    # Repeated reads reuse the first result; the recovery response is not executed.
    assert result.model_requests == 4 and len(executions) == 1
    assert len({request.request_chain_id for request in provider.requests}) == 1
    sequence_key = "messages" if protocol == "chat_completions" else "input"
    settings = [
        {key: value for key, value in payload.items() if key != sequence_key} for payload in wire
    ]
    assert all(item == settings[0] for item in settings)
    assert all(request.tool_choice == "auto" for request in provider.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_anchor", [False, True])
async def test_compaction_is_local_fence_before_any_tool_execution(
    database, tmp_path, monkeypatch, with_anchor
):
    control = await _control(database, tmp_path)
    task = ChatMessage("user", "actual task")
    fixed = (ChatTool("read_probe", "Read audit data", {"type": "object"}),)
    provider = FakeLLMProvider(
        lambda _: ChatResponse(
            "ignoring compaction instruction",
            0,
            tool_calls=(ToolCall("not-executed", ToolFunction("read_probe", "{}")),),
        )
    )
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    backend = SimpleNamespace(
        definitions=lambda *args, **kwargs: fixed,
        execute=AsyncMock(side_effect=AssertionError("compaction must not execute tools")),
    )
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="compaction-fence",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        fixed_tools=fixed,
        work_control=control,
        compaction_brief=task if with_anchor else None,
    )
    monkeypatch.setattr(WorkSession, "needs_compaction", AsyncMock(return_value=True))
    # Run owns session creation; the legacy no-anchor case must fail before dispatch.
    result = await chat._agent_runner.run((ChatMessage("system", "fixed"), task), runtime, backend)
    assert result.work_state == "suspended"
    assert result.outcome.failure.code == ("ValueError" if with_anchor else "JournalUnavailable")
    backend.execute.assert_not_awaited()
    assert len(provider.requests) == (1 if with_anchor else 0)
    if with_anchor:
        assert provider.requests[0].tools == fixed
        assert provider.requests[0].tool_choice == "auto"
    row = await control.repository.get(control.current["id"])
    assert row["tool_calls"] == 0
    assert row["model_requests"] == (1 if with_anchor else 0)
    await control.repository.release(control.lease)


@pytest.mark.asyncio
async def test_main_entry_captures_current_task_before_work_status(database, tmp_path):
    control = await _control(database, tmp_path)
    runner = SimpleNamespace(run=AsyncMock(return_value=None))
    turns = MainAgentTurnService(
        SimpleNamespace(_settings=SimpleNamespace(runtime_work_enabled=False)), runner
    )
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider())
    chat = harness.processor._chat
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="main",
        current_group_id=None,
        bot_user_id="9999",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        work_control=control,
    )
    current = ChatMessage("user", "current task plus dynamic context")
    await turns.run(
        (ChatMessage("system", "fixed"), ChatMessage("user", "history"), current), runtime, None
    )
    messages, prepared, _backend = runner.run.call_args.args
    assert prepared.compaction_brief == current
    assert messages[-2] == current and "运行状态资料" in messages[-1].content
    await control.repository.release(control.lease)
