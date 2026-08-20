"""R4 conversation runtime regressions that must not depend on Planner LLM."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.admin.models import (
    AgentRuntimeConfig,
    ContextRuntimeConfig,
    ConversationRuntimeConfig,
    EmojiRuntimeConfig,
    LLMRuntimeConfig,
    MemoryRetrievalRuntimeConfig,
    PluginRuntimeConfig,
    RelationshipRuntimeConfig,
    ReplyRuntimeConfig,
    RuntimeConfigSnapshot,
    SpeechRuntimeConfig,
    VisionRuntimeConfig,
    WebRuntimeConfig,
)
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.cadence import ReplyEffectRepository, conversation_key_hash
from qq_ai_bot.conversation.delivery import ReplyControlState, default_reply_spec
from qq_ai_bot.conversation.participation import (
    AdmissionFeatures,
    LocalAutonomousParticipationPolicy,
)
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatResponse,
    ChatTool,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.emoji.models import (
    EmojiPlacement,
    EmojiPreparationResult,
    EmojiPreparationStatus,
    EmojiReplyMode,
    PendingReplyEffect,
)
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.observability import stable_identifier_hash
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime
from qq_ai_bot.services.agent_tools import _RUNTIME_SNAPSHOT, AgentToolService, ToolRuntime
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.policies import evaluate_message, replies_to_bot
from qq_ai_bot.speech.reply_effect import PendingVoiceReplyEffect
from qq_ai_bot.time.models import TimeContext


def _runtime() -> RuntimeConfigSnapshot:
    """Build a Conversation Runtime snapshot without Planner dual-read keys."""

    return RuntimeConfigSnapshot(
        conversation=ConversationRuntimeConfig(
            autonomous_enabled=True,
            autonomous_debounce_seconds=3.0,
            autonomous_admission_threshold=80,
            autonomous_batch_limit=20,
            autonomous_presence_window_seconds=300,
            interrupt_autonomous_on_new_message=True,
        ),
        plugins=PluginRuntimeConfig(
            hook_timeout_seconds=3,
            max_prompt_fragment_characters=2000,
            max_prompt_characters_per_plugin=4000,
            max_total_prompt_characters=8000,
        ),
        context=ContextRuntimeConfig(local_event_limit=30),
        memory=MemoryRetrievalRuntimeConfig(
            retrieval_enabled=True,
            max_referenced_targets=5,
            lexical_candidate_limit=50,
            context_limit_per_entity=8,
            overview_limit_per_entity=20,
            always_on_explicit_preference_limit=3,
            query_term_limit=12,
            short_query_fallback_enabled=True,
        ),
        reply=ReplyRuntimeConfig(
            delay_min_seconds=3,
            delay_max_seconds=5,
            max_qq_message_chars=1500,
            cancel_on_new_message=True,
            hard_max_messages=10,
        ),
        llm=LLMRuntimeConfig(
            model="main-model",
            timeout_seconds=30,
            max_retries=1,
            temperature=0.7,
            max_output_tokens=2048,
            thinking_enabled=True,
        ),
        agent=AgentRuntimeConfig(
            max_tool_calls=5,
            max_model_requests=6,
            tool_result_max_characters=32_000,
        ),
        web=WebRuntimeConfig(
            search_max_results=5,
            extract_max_results=3,
            max_calls_per_turn=3,
            tool_result_max_characters=20_000,
            source_retention_days=7,
            source_max_runs_per_conversation=10,
        ),
        relationship=RelationshipRuntimeConfig(
            confidence_threshold=0.7,
            max_auto_delta=5,
            daily_positive_cap=10,
            daily_negative_cap=-10,
            conflict_preference_min_gap=10,
            initial_affection=50,
            initial_trust=50,
        ),
        vision=VisionRuntimeConfig(
            max_images_per_turn=10,
            max_frames_per_turn=10,
            gif_max_frames=8,
            thinking_enabled=False,
            thinking_budget=0,
            low_confidence_retry_threshold=0.5,
            per_user_requests_per_minute=10,
            per_group_requests_per_minute=30,
            analysis_retention_days=7,
        ),
        emoji=EmojiRuntimeConfig(
            enabled=True,
            collection_enabled=True,
            collection_mode="likely",
            collect_private=True,
            collect_group=True,
            auto_adopt_enabled=True,
            auto_adopt_min_confidence=0.78,
            pool_capacity=None,
            replacement_mode="score",
            selector_enabled=True,
            selector_candidate_count=3,
            selector_score_gap=0.75,
            selector_timeout_seconds=2,
            max_effects_per_reply=1,
            spontaneous_frequency=0.15,
            near_duplicate_enabled=True,
            near_duplicate_distance=6,
            same_emoji_cooldown_seconds=300,
            scope_repeat_cooldown_seconds=60,
            cache_retention_days=30,
            worker_batch_size=10,
            worker_poll_seconds=2,
            worker_lease_seconds=120,
            worker_max_attempts=3,
            worker_retry_delay_seconds=30,
            analysis_version="emoji-v1",
        ),
        speech=SpeechRuntimeConfig(
            enabled=False,
            provider="genie",
            socket_path="/run/yuki-speech/genie.sock",
            root="/data/speech",
            genie_data_dir="/data/speech/genie_data",
            default_profile="",
            agent_effects_enabled=True,
            default_mode="optional",
            split_sentence=True,
            max_synthesis_characters=None,
            queue_max_pending=None,
            cache_retention_hours=None,
            private_enabled=True,
            group_enabled=True,
            automation_enabled=True,
            plugin_enabled=True,
            text_fallback_enabled=True,
        ),
    )


def _inbound(
    *,
    group: bool = True,
    reply_to_bot: bool = False,
    mention: bool = False,
) -> InboundMessage:
    return InboundMessage(
        message_id="1",
        event_type="message:group:normal" if group else "message:private",
        scope_type=ScopeType.GROUP if group else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="你好",
        bot_user_id="9999",
        group_id="2001" if group else None,
        mentions_bot=mention,
        reply_sender_user_id="9999" if reply_to_bot else None,
        reply_to_message_id="10" if reply_to_bot else None,
    )


def test_private_mention_and_reply_to_bot_are_direct_without_scoring() -> None:
    settings = Settings(superusers_csv="1", enabled_groups_csv="2001")
    private = evaluate_message(_inbound(group=False), settings=settings)
    reply = evaluate_message(_inbound(reply_to_bot=True), settings=settings)
    mention = evaluate_message(_inbound(mention=True), settings=settings)
    assert private.should_respond and private.reason == "private_allowed"
    assert reply.should_respond and reply.reason == "group_reply_to_bot"
    assert mention.should_respond and mention.reason == "group_triggered"
    assert replies_to_bot(_inbound(reply_to_bot=True))
    assert not replies_to_bot(_inbound(reply_to_bot=False))


def test_disabled_group_is_rejected() -> None:
    decision = evaluate_message(
        _inbound(group=True),
        settings=Settings(enabled_groups_csv=""),
    )
    assert not decision.should_respond
    assert decision.reason == "group_disabled"


def test_autonomous_scorer_ignores_direct_admission_flags() -> None:
    scorer = LocalAutonomousParticipationPolicy()
    plain = scorer.evaluate(AdmissionFeatures(scope_type=ScopeType.GROUP, text="请帮我查一下"))
    boosted = scorer.evaluate(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="请帮我查一下",
            mentions_bot=True,
            reply_target_is_bot=True,
        )
    )
    assert plain.score == boosted.score
    assert "mentions_bot" not in boosted.reasons
    assert "reply_to_bot" not in boosted.reasons


def test_low_value_group_observation_does_not_participate() -> None:
    snapshot = LocalAutonomousParticipationPolicy().evaluate(
        AdmissionFeatures(scope_type=ScopeType.GROUP, text="哈哈")
    )
    assert not snapshot.should_participate
    assert snapshot.score < snapshot.threshold


def test_fast_conversation_presence_penalty_is_applied() -> None:
    snapshot = LocalAutonomousParticipationPolicy().evaluate(
        AdmissionFeatures(
            scope_type=ScopeType.GROUP,
            text="请帮我查一下这是什么？",
            average_human_interval_seconds=1.0,
            recent_bot_messages=4,
            recent_total_messages=6,
            seconds_since_last_bot_message=10,
        )
    )
    assert snapshot.activity_penalty > 0
    assert snapshot.presence_penalty > 0
    assert "conversation_fast" in snapshot.reasons
    assert "recent_bot_presence" in snapshot.reasons


def test_cadence_conversation_hash_matches_stable_identifier() -> None:
    key = "group:2001"
    assert conversation_key_hash(key) == stable_identifier_hash(key, kind="conversation")


def test_conversation_policy_reads_conversation_keys() -> None:
    snapshot = _runtime()
    policy = snapshot.conversation_policy()
    assert policy is snapshot.conversation
    assert isinstance(policy, ConversationRuntimeConfig)
    assert policy.autonomous_enabled is True
    assert policy.autonomous_admission_threshold == 80
    assert snapshot.reply.hard_max_messages == 10
    assert snapshot.speech.agent_effects_enabled is True


def _tool_service() -> AgentToolService:
    token = _RUNTIME_SNAPSHOT.set(_runtime())
    service = object.__new__(AgentToolService)
    service._runtime_token = token
    return service


def test_decline_reply_rejected_on_direct_origin() -> None:
    service = _tool_service()
    runtime = ToolRuntime(
        inbound=_inbound(group=False),
        gateway=None,
        allow_generic_onebot=False,
        origin=TurnOrigin.USER_MESSAGE,
        reply_control=ReplyControlState(spec=default_reply_spec(hard_max_messages=3)),
    )
    payload = json.loads(service._decline_reply({"reason_code": "not_relevant"}, runtime))
    assert payload["ok"] is False
    assert payload["error"] == "decline_reply_forbidden"


def test_one_shot_send_voice_does_not_write_preference() -> None:
    service = _tool_service()
    service._voice_preferences = SimpleNamespace(set_persistent=lambda **_kwargs: None)
    runtime = ToolRuntime(
        inbound=_inbound(group=False),
        gateway=None,
        allow_generic_onebot=False,
        origin=TurnOrigin.USER_MESSAGE,
        reply_effects=[],
        reply_control=ReplyControlState(spec=default_reply_spec(hard_max_messages=3)),
        runtime_config=SimpleNamespace(
            speech=SimpleNamespace(
                enabled=True,
                agent_effects_enabled=True,
                private_enabled=True,
                group_enabled=True,
            )
        ),
    )
    payload = json.loads(
        service._queue_voice(
            {"mode": "text_and_voice", "request_basis": "user_requested"},
            runtime,
        )
    )
    assert payload["ok"] is True
    assert len(runtime.reply_effects or ()) == 1
    assert isinstance((runtime.reply_effects or ())[0], PendingVoiceReplyEffect)


def test_send_voice_unavailable_when_speech_disabled() -> None:
    service = _tool_service()
    runtime = ToolRuntime(
        inbound=_inbound(group=False),
        gateway=None,
        allow_generic_onebot=False,
        origin=TurnOrigin.USER_MESSAGE,
        reply_effects=[],
        runtime_config=SimpleNamespace(
            speech=SimpleNamespace(
                enabled=False,
                agent_effects_enabled=True,
                private_enabled=True,
                group_enabled=True,
            )
        ),
    )
    payload = json.loads(
        service._queue_voice(
            {"mode": "voice_only", "request_basis": "user_requested"},
            runtime,
        )
    )
    assert payload["ok"] is False
    assert payload["error"] == "speech_unavailable"


class _DeclineThenTextProvider(LLMProvider):
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def complete(self, request: object) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="decline-1",
                        function=ToolFunction(
                            name="decline_reply",
                            arguments='{"reason_code":"not_relevant"}',
                        ),
                    ),
                ),
            )
        return ChatResponse(content="should not continue", latency_seconds=0)


class _MixedDeclineProvider(LLMProvider):
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def complete(self, request: object) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="decline-1",
                        function=ToolFunction(
                            name="decline_reply",
                            arguments='{"reason_code":"not_relevant"}',
                        ),
                    ),
                    ToolCall(
                        id="emoji-1",
                        function=ToolFunction(
                            name="send_emoji",
                            arguments='{"mode":"with_text","placement":"after_text","goal":"x"}',
                        ),
                    ),
                ),
            )
        return ChatResponse(content="mixed decline rejected", latency_seconds=0)


class _DeclineBackend:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self._declined = False

    def definitions(self, runtime: object, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        del runtime, web_was_used
        return (
            ChatTool(
                name="decline_reply",
                description="decline",
                parameters={"type": "object", "properties": {}},
            ),
            ChatTool(
                name="send_emoji",
                description="emoji",
                parameters={"type": "object", "properties": {}},
            ),
        )

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: object) -> None:
        del calls, runtime

    async def execute(self, name: str, arguments_json: str, runtime: object) -> str:
        del arguments_json, runtime
        self.executed.append(name)
        if name == "decline_reply":
            self._declined = True
            return json.dumps({"ok": True, "data": {"declined": True}})
        return json.dumps({"ok": True})

    def finalize(self, content: str, runtime: object) -> str:
        del runtime
        return content

    def exhausted(self, runtime: object) -> str:
        del runtime
        return "exhausted"

    def declined_reply(self) -> bool:
        return self._declined

    def has_prior_reply_effects(self) -> bool:
        return False


def _agent_runtime(*, origin: TurnOrigin = TurnOrigin.AUTONOMOUS_GROUP) -> AgentRuntime:
    now = datetime.now(UTC)
    return AgentRuntime(
        origin=origin,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="group:2001",
        current_group_id="2001",
        bot_user_id="8000",
        gateway=None,
        runtime_config=_runtime(),
        current_time=TimeContext(utc=now, local=now, timezone="Asia/Shanghai"),
        allowed_capabilities=frozenset({"decline_reply", "send_emoji"}),
        max_tool_calls=5,
        max_model_requests=6,
    )


@pytest.mark.asyncio
async def test_decline_reply_is_terminal_with_no_continuation() -> None:
    provider = _DeclineThenTextProvider()
    backend = _DeclineBackend()
    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="群里随便聊聊"),),
        _agent_runtime(),
        backend,
    )
    assert result.suppress_delivery is True
    assert result.model_requests == 1
    assert len(provider.requests) == 1
    assert backend.executed == ["decline_reply"]


@pytest.mark.asyncio
async def test_decline_reply_mixed_batch_is_rejected_without_side_effects() -> None:
    provider = _MixedDeclineProvider()
    backend = _DeclineBackend()
    result = await AgentRunner(provider, ConcurrencyManager(1)).run(
        (ChatMessage(role="user", content="群里随便聊聊"),),
        _agent_runtime(),
        backend,
    )
    assert backend.executed == []
    assert result.suppress_delivery is False
    assert len(provider.requests) >= 1


@pytest.mark.asyncio
async def test_ordinary_text_uses_exactly_one_agent_request_and_zero_planner(
    database: Database,
) -> None:
    provider = FakeLLMProvider(lambda _request: "你好呀")
    harness = build_harness(database, make_settings(database.url), provider)
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("hello", message_id="r4-gold"),
        sender,
    )
    assert result.reason == "chat"
    assert len(provider.requests) == 1
    assert sender.messages[0].text == "你好呀"
    assert not hasattr(harness.processor, "_planner")
    assert provider.requests[0].tool_choice != "required"


@pytest.mark.asyncio
async def test_plugin_background_reply_exposes_no_business_tools(database: Database) -> None:
    seen: list[tuple[str, ...]] = []

    def responder(request: object) -> ChatResponse:
        tools = getattr(request, "tools", ())
        seen.append(tuple(tool.name for tool in tools))
        return ChatResponse(content="该喝水了", latency_seconds=0)

    harness = build_harness(database, make_settings(database.url), FakeLLMProvider(responder))
    chat = harness.processor._chat
    runtime = await chat._runtime_config.snapshot(user_id="1001")
    scope = ConversationScope.private("9999", "1001")
    appended = await harness.processor._scoped_events.append_external(
        scope=scope,
        platform_message_id="ext-1",
        source_plugin_id="demo",
        external_source="plugin",
        external_event_key="reminder-1",
        external_event_type="reminder",
        external_payload={},
        external_target_id="1001",
        content="插件提醒：该喝水了",
        occurred_at=datetime.now(UTC),
    )
    token = await chat._turn_coordinator.begin_background(scope.key)
    assert token is not None
    result = await chat.generate_external_reply(
        event=appended.event,
        authorization_user_id="1001",
        runtime=runtime,
        agent_intent="提醒用户喝水",
        turn_token=token,
        turn_snapshot=ConversationTurnSnapshot(
            scope_id=appended.scope.id,
            scope_key=scope.key,
            generation=appended.scope.generation,
            trigger_event_id=appended.event.id,
            coordinator_version=token.version,
        ),
    )
    assert result.text == "该喝水了"
    assert seen
    names = {name for batch in seen for name in batch}
    assert {"request_tools", "set_reply_target"} <= names
    assert names.isdisjoint({"send_emoji", "send_voice", "memory_change"})


@pytest.mark.asyncio
async def test_plugin_background_rejects_declared_tool_calls(database: Database) -> None:
    calls = {"n": 0}

    def responder(request: object) -> ChatResponse:
        tools = getattr(request, "tools", ())
        names = {tool.name for tool in tools}
        if calls["n"] == 0 and "request_tools" in names:
            calls["n"] += 1
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="req-1",
                        function=ToolFunction(
                            name="request_tools",
                            arguments=json.dumps({"query": "web_search", "max_results": 1}),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="该喝水了", latency_seconds=0)

    harness = build_harness(database, make_settings(database.url), FakeLLMProvider(responder))
    chat = harness.processor._chat
    runtime = await chat._runtime_config.snapshot(user_id="1001")
    scope = ConversationScope.private("9999", "1001")
    appended = await harness.processor._scoped_events.append_external(
        scope=scope,
        platform_message_id="ext-2",
        source_plugin_id="demo",
        external_source="plugin",
        external_event_key="reminder-2",
        external_event_type="reminder",
        external_payload={},
        external_target_id="1001",
        content="插件提醒：该喝水了",
        occurred_at=datetime.now(UTC),
    )
    token = await chat._turn_coordinator.begin_background(scope.key)
    assert token is not None
    result = await chat.generate_external_reply(
        event=appended.event,
        authorization_user_id="1001",
        runtime=runtime,
        agent_intent="提醒用户喝水",
        turn_token=token,
        turn_snapshot=ConversationTurnSnapshot(
            scope_id=appended.scope.id,
            scope_key=scope.key,
            generation=appended.scope.generation,
            trigger_event_id=appended.event.id,
            coordinator_version=token.version,
        ),
    )
    assert result.text == "该喝水了"
    assert calls["n"] == 1
    assert result.tool_calls_used == 1
    assert result.model_requests == 2


@pytest.mark.asyncio
async def test_user_requested_voice_is_excluded_from_cadence_denominator(
    database: Database,
) -> None:
    repo = ReplyEffectRepository(database)
    await repo.record(
        conversation_key="private:1001",
        source_event_id="user-voice",
        text_sent=True,
        voice_sent=True,
        emoji_sent=False,
        voice_request_basis="user_requested",
    )
    await repo.record(
        conversation_key="private:1001",
        source_event_id="plain-text",
        text_sent=True,
        voice_sent=False,
        emoji_sent=False,
        voice_request_basis="none",
    )
    await repo.record(
        conversation_key="private:1001",
        source_event_id="agent-voice",
        text_sent=True,
        voice_sent=True,
        emoji_sent=False,
        voice_request_basis="agent_initiated",
    )
    await repo.record(
        conversation_key="private:1001",
        source_event_id="opt-out",
        text_sent=True,
        voice_sent=False,
        emoji_sent=False,
        voice_request_basis="none",
        voice_cadence_eligible=False,
    )
    cadence = await repo.voice_cadence("private:1001")
    assert cadence.eligible_turns == 2
    assert cadence.voice_turns == 1
    assert cadence.ratio == 0.5


def test_send_emoji_emoji_only_is_exclusive_visible_output() -> None:
    service = _tool_service()
    runtime = ToolRuntime(
        inbound=_inbound(group=False),
        gateway=None,
        allow_generic_onebot=False,
        origin=TurnOrigin.USER_MESSAGE,
        reply_effects=[],
        reply_control=ReplyControlState(spec=default_reply_spec(hard_max_messages=3)),
        runtime_config=SimpleNamespace(emoji=SimpleNamespace(enabled=True)),
    )
    payload = json.loads(
        service._queue_emoji(
            {"mode": "emoji_only", "placement": "after_text", "goal": "回应晚安"},
            runtime,
        )
    )
    assert payload["ok"] is True
    effect = (runtime.reply_effects or ())[0]
    assert isinstance(effect, PendingReplyEffect)
    assert effect.mode is EmojiReplyMode.EMOJI_ONLY
    assert effect.placement is EmojiPlacement.ONLY
    assert effect.explicit_request is True


def test_optional_emoji_failure_keeps_agent_text() -> None:
    optional = PendingReplyEffect(
        mode=EmojiReplyMode.PREFERRED,
        placement=EmojiPlacement.AFTER_TEXT,
        goal="轻松一下",
        explicit_request=True,
        source="agent",
    )
    exclusive = PendingReplyEffect(
        mode=EmojiReplyMode.EMOJI_ONLY,
        placement=EmojiPlacement.ONLY,
        goal="只发表情",
        explicit_request=True,
        source="agent",
    )
    failed = EmojiPreparationResult(
        status=EmojiPreparationStatus.NO_CANDIDATE,
        reason_code="no_candidate",
    )
    optional_fallback = ChatService._emoji_preparation_failure_text(optional, failed)
    exclusive_fallback = ChatService._emoji_preparation_failure_text(exclusive, failed)
    assert optional_fallback == "表情没发出去，先用文字回你。"
    assert exclusive_fallback == "我这边暂时没有可用的表情。"
    assert optional.mode is not EmojiReplyMode.EMOJI_ONLY
    assert optional.placement is not EmojiPlacement.ONLY
