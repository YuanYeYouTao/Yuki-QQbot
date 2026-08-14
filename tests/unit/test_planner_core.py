from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from qq_ai_bot.admin.models import (
    AgentRuntimeConfig,
    ContextRuntimeConfig,
    EmojiRuntimeConfig,
    LLMRuntimeConfig,
    MemoryRetrievalRuntimeConfig,
    PlannerRuntimeConfig,
    PluginRuntimeConfig,
    RelationshipRuntimeConfig,
    ReplyRuntimeConfig,
    RuntimeConfigSnapshot,
    SpeechRuntimeConfig,
    VisionRuntimeConfig,
    WebRuntimeConfig,
)
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage, SenderIdentity
from qq_ai_bot.emoji.models import EmojiIntent, EmojiPlacement, EmojiReplyMode, EmojiReplyPlan
from qq_ai_bot.emoji.request_detector import EmojiRequestDetector
from qq_ai_bot.llm.base import LLMUnavailableError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.enums import MemoryContextMode
from qq_ai_bot.model_runtime.structured import _compact_json_schema
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.planner import (
    DeliveryMode,
    FakePlannerProvider,
    LLMPlannerProvider,
    MemoryContextPlan,
    MemoryContextReasonCode,
    PlannedTurn,
    PlannerDecision,
    PlannerInput,
    PlannerInterruptedError,
    PlannerObservability,
    PlannerReasonCode,
    PlannerResponseError,
    PlannerSignal,
    ReplyNecessityFeatures,
    ReplyNecessityScorer,
    ToolMode,
    TurnPlan,
    constrain_turn_plan,
)
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.models import (
    PlannerEmojiContext,
    PlannerMemoryOutput,
    PlannerModelOutput,
    PlannerSpeechContext,
    PlannerToolOutput,
    ToolScopeSummary,
)
from qq_ai_bot.planner.prompt import PLANNER_SYSTEM_PROMPT, planner_payload
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.speech.models import (
    SpeechLanguageHint,
    VoiceAgentToolPolicy,
    VoiceIntent,
    VoiceMode,
    VoicePreferenceChange,
    VoicePreferenceDuration,
    VoicePreferenceMode,
    VoiceReplyPlan,
)


def _walk_json_dicts(value: object) -> Iterator[dict[object, object]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_dicts(child)


def _runtime() -> RuntimeConfigSnapshot:
    return RuntimeConfigSnapshot(
        planner=PlannerRuntimeConfig(
            direct_enabled=True,
            group_enabled=True,
            temperature=0.1,
            max_output_tokens=512,
            timeout_seconds=20,
            confidence_threshold=0.65,
            reply_necessity_threshold=80,
            max_pending_messages=20,
            recent_presence_window_seconds=300,
            max_wait_seconds=60,
            interrupt_autonomous_on_new_message=True,
            record_runs=True,
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
            plan_hard_max_messages=10,
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
            planner_enabled=True,
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


def test_self_recall_defaults_closed_and_prompt_has_strict_examples() -> None:
    output = PlannerMemoryOutput(
        access="automatic",
        mode=MemoryContextMode.HYBRID,
        purpose="background",
    )
    assert output.materialize().self_recall is False
    opened = PlannerMemoryOutput.model_validate(
        {
            "access": "automatic",
            "mode": MemoryContextMode.HYBRID,
            "purpose": "recall",
            "reason_code": MemoryContextReasonCode.SELF_MEMORY_RECALL,
            "self_recall": True,
        }
    ).materialize()
    assert opened.self_recall is True
    assert opened.reason_code is MemoryContextReasonCode.SELF_MEMORY_RECALL
    assert "你喜欢咖啡吗" in PLANNER_SYSTEM_PROMPT
    assert "帮我查天气" in PLANNER_SYSTEM_PROMPT
    assert "X 还是 Y" in PLANNER_SYSTEM_PROMPT
    assert "即使句子中出现“记得”也不要误判成 recall" in PLANNER_SYSTEM_PROMPT
    assert "constraint=strict" in PLANNER_SYSTEM_PROMPT

    strict = PlannerMemoryOutput.model_validate(
        {
            "access": "automatic",
            "mode": "hybrid",
            "purpose": "recall",
            "temporal": {
                "mode": "range",
                "constraint": "strict",
                "start_at": "2026-08-01T00:00:00+08:00",
                "end_at": "2026-08-10T23:59:59+08:00",
            },
        }
    ).materialize()
    assert strict.temporal.constraint.value == "strict"
    assert strict.temporal.start_at == datetime(2026, 7, 31, 16, tzinfo=UTC)


def test_planner_memory_access_contract_and_requested_count_are_strict() -> None:
    automatic = PlannerMemoryOutput.model_validate(
        {
            "access": "automatic",
            "mode": "overview",
            "purpose": "recall",
            "requested_count": 2,
        }
    ).materialize()
    tool = PlannerMemoryOutput.model_validate(
        {"access": "tool", "mode": "none", "purpose": "recall"}
    ).materialize()

    assert automatic.requested_count == 2
    assert automatic.to_query_intent().requested_count == 2
    assert tool.mode is MemoryContextMode.NONE
    with pytest.raises(ValueError, match="automatic memory access requires a retrieval mode"):
        PlannerMemoryOutput.model_validate(
            {"access": "automatic", "mode": "none", "purpose": "recall"}
        ).materialize()
    with pytest.raises(ValueError, match="none/tool memory access requires mode=none"):
        PlannerMemoryOutput.model_validate(
            {"access": "tool", "mode": "lexical", "purpose": "recall"}
        ).materialize()


@pytest.mark.asyncio
async def test_invalid_memory_access_mode_combination_uses_whole_plan_fallback() -> None:
    payload = _valid_plan_payload()
    payload["memory_context"] = {
        "access": "tool",
        "mode": "hybrid",
        "purpose": "recall",
    }
    provider = LLMPlannerProvider(
        FakeLLMProvider(lambda _request: json.dumps(payload)),
        model="planner-model",
    )

    plan = await provider.plan(_planner_input(), runtime=_runtime())

    assert plan.confidence == 0
    assert plan.memory_context.access.value == "automatic"
    assert plan.memory_context.mode is MemoryContextMode.LEXICAL


def test_planner_prompt_requires_explicit_scope_and_query_rewrite_for_tool_tasks() -> None:
    assert "必须输出 tool_selection 并选最小 scopes" in PLANNER_SYSTEM_PROMPT
    assert "仅 scope\n不明时省略" in PLANNER_SYSTEM_PROMPT
    assert "intent 必须用一句短而规范化" in PLANNER_SYSTEM_PROMPT
    assert "已有合适工具禁用它" in PLANNER_SYSTEM_PROMPT
    assert "scopes 不是权限边界" in PLANNER_SYSTEM_PROMPT
    description = PlannerToolOutput.model_fields["scopes"].description or ""
    assert "capabilities.tool_scopes" in description
    assert "available_tool_scopes" not in description


def _planner_input(
    *,
    scope: ScopeType = ScopeType.PRIVATE,
    origin: TurnOrigin = TurnOrigin.USER_MESSAGE,
    text: str = "帮我看看",
    mentions_bot: bool = False,
    reply_target_is_bot: bool = False,
    visual: bool = False,
) -> PlannerInput:
    scorer = ReplyNecessityScorer()
    necessity = scorer.score(
        ReplyNecessityFeatures(
            scope_type=scope,
            text=text,
            mentions_bot=mentions_bot,
            reply_target_is_bot=reply_target_is_bot,
        )
    )
    current = ChatMessage(
        role="user",
        content=f"[测试用户|QQ:1001]\n#101>{text}",
    )
    return PlannerInput(
        conversation_key="private:1001" if scope is ScopeType.PRIVATE else "group:2001:user:1001",
        scope_type=scope,
        origin=origin,
        trigger_message_id="101",
        trigger_event_id=101,
        bot_user_id="9999",
        current_sender_user_id="1001",
        current_group_id=None if scope is ScopeType.PRIVATE else "2001",
        history_messages=(
            ChatMessage(
                role="user",
                content="[历史用户|QQ:1002]\n#100>earlier",
            ),
        ),
        current_message=current,
        current_message_text=text,
        trusted_history_sender_user_ids=("1002",),
        trusted_history_event_ids=(100,),
        reply_target_is_bot=reply_target_is_bot,
        mentions_bot=mentions_bot,
        mentioned_user_ids=("1002",),
        visual_input_present=visual,
        current_time=datetime.now(UTC),
        necessity=necessity,
        available_tool_categories=("history", "admin"),
    )


def _valid_plan_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": "reply",
        "intent": "回答当前问题",
        "target_user_ids": ["1001"],
        "delivery_mode": "single",
        "desired_messages": 1,
        "reply_to_event_id": None,
        "tool_selection": {"mode": "inherit", "scopes": []},
        "wait_seconds": 0,
        "confidence": 0.9,
        "reason_code": "direct_request",
        "memory_context": {
            "access": "automatic",
            "mode": "lexical",
            "purpose": "background",
            "reason_code": "routine_context",
        },
        "emoji": {"intent": "neutral", "mode": "none"},
        "voice": {"mode": "text", "intent": "neutral"},
    }
    payload.update(updates)
    return payload


def test_planner_payload_is_compact_stable_and_excludes_backend_ids() -> None:
    planner_input = _planner_input().model_copy(
        update={
            "available_tool_scopes": (
                ToolScopeSummary(
                    scope_id="web",
                    display_name="Web",
                    description="联网读取",
                    tool_count=4,
                    provider_ids=("internal",),
                    tags=("network",),
                ),
                ToolScopeSummary(
                    scope_id="automation",
                    display_name="Automation",
                    description="延后执行",
                    tool_count=3,
                ),
            ),
            "plugin_signals": (
                PlannerSignal(
                    source_plugin_id="z-plugin",
                    score_delta=1.0,
                    reason_code="z",
                ),
                PlannerSignal(
                    source_plugin_id="a-plugin",
                    score_delta=2.0,
                    reason_code="a",
                ),
            ),
        }
    )

    payload = planner_payload(planner_input)

    assert list(payload) == [
        "capabilities",
        "history_messages",
        "conversation_state",
        "current_message",
        "current_time",
    ]
    capabilities = payload["capabilities"]
    assert isinstance(capabilities, dict)
    assert capabilities["tool_scopes"] == [
        {"scope_id": "automation", "description": "延后执行"},
        {"scope_id": "web", "description": "联网读取"},
    ]
    conversation_state = payload["conversation_state"]
    assert isinstance(conversation_state, dict)
    signals = conversation_state["plugin_signals"]
    assert isinstance(signals, list)
    assert [signal["source"] for signal in signals] == ["a-plugin", "z-plugin"]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert "private:1001" not in encoded
    assert '"trigger_message_id"' not in encoded
    assert '"current_sender_user_id"' not in encoded
    assert '"provider_ids"' not in encoded
    assert '"tool_count"' not in encoded


@pytest.mark.asyncio
async def test_planner_history_excludes_the_separate_current_message() -> None:
    now = datetime.now(UTC)
    previous = EventRecord(
        id=1,
        bot_user_id="9999",
        platform_message_id="previous",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="9999",
        direction="outbound",
        content="上一条回复",
        visual_summary="",
        segments=(),
        occurred_at=now - timedelta(seconds=10),
        private_peer_user_id="1001",
    )
    current = EventRecord(
        id=2,
        bot_user_id="9999",
        platform_message_id="current",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        sender_nickname="远野",
        direction="inbound",
        content="有时候还要学会调用工具",
        visual_summary="",
        segments=(),
        occurred_at=now,
        private_peer_user_id="1001",
    )
    ledger = AsyncMock()
    ledger.list_recent.return_value = (previous, current)
    relationships = AsyncMock()
    relationships.get.return_value = None
    builder = PlannerContextBuilder(ledger=ledger, relationships=relationships)
    inbound = InboundMessage(
        message_id="current",
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001", nickname="远野"),
        text=current.content,
        bot_user_id="9999",
        received_at=now,
    )

    planner_input = await builder.build(
        inbound=inbound,
        conversation_key="private:1001",
        content=current.content,
        origin=TurnOrigin.USER_MESSAGE,
        runtime=_runtime(),
        now=now,
    )

    assert [message.role for message in planner_input.history_messages] == ["assistant"]
    assert planner_input.history_messages[0].content == ("[Yuki|QQ:9999]\n#1>上一条回复")
    assert planner_input.current_message.content == ("[远野|QQ:1001]\n#2>有时候还要学会调用工具")
    assert planner_input.known_event_ids == (1, 2)
    assert planner_input.necessity.pending_message_count == 1


@pytest.mark.asyncio
async def test_planner_receives_ten_continuous_history_messages_plus_current() -> None:
    now = datetime.now(UTC)
    history = tuple(
        EventRecord(
            id=index,
            bot_user_id="9999",
            platform_message_id=str(index),
            scope_type=ScopeType.GROUP,
            sender_user_id=f"10{index:02d}",
            sender_group_card=f"群友{index}",
            direction="inbound",
            content=f"历史消息{index}",
            visual_summary="",
            segments=(),
            occurred_at=now - timedelta(seconds=13 - index),
            group_id="2001",
        )
        for index in range(1, 13)
    )
    current = EventRecord(
        id=13,
        bot_user_id="9999",
        platform_message_id="13",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        sender_group_card="远野",
        direction="inbound",
        content="当前消息",
        visual_summary="",
        segments=(),
        occurred_at=now,
        group_id="2001",
    )
    ledger = AsyncMock()
    ledger.list_recent.return_value = (*history, current)
    relationships = AsyncMock()
    relationships.get.return_value = None
    builder = PlannerContextBuilder(ledger=ledger, relationships=relationships)
    inbound = InboundMessage(
        message_id="13",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", group_card="远野"),
        text="当前消息",
        bot_user_id="9999",
        group_id="2001",
        mentions_bot=True,
        received_at=now,
    )

    planner_input = await builder.build(
        inbound=inbound,
        conversation_key="group:2001",
        content=inbound.text,
        origin=TurnOrigin.USER_MESSAGE,
        runtime=_runtime(),
        now=now,
    )

    assert len(planner_input.history_messages) == 10
    assert planner_input.trusted_history_event_ids == tuple(range(3, 13))
    assert "#13>" not in "\n".join(
        message.content or "" for message in planner_input.history_messages
    )
    assert "#13>" in (planner_input.current_message.content or "")
    assert ledger.list_recent.await_args.kwargs["limit"] == 11


@pytest.mark.parametrize(
    ("text", "standalone"),
    (
        ("@Yuki 发个表情", True),
        ("来个开心的表情包", True),
        ("给我发张梗图", True),
        ("这个表情是什么意思", False),
        ("不要发表情", False),
        ("回答问题并带一个表情", False),
        ("给图片加表情", False),
    ),
)
def test_emoji_request_detector_is_conservative(text: str, standalone: bool) -> None:
    hint = EmojiRequestDetector().detect(text)
    assert hint.standalone_request is standalone
    assert hint.explicit_request is standalone


def test_emoji_request_detector_accepts_configured_bot_alias() -> None:
    hint = EmojiRequestDetector(("Mika", "米卡")).detect("米卡，发个开心的表情")

    assert hint.standalone_request
    assert hint.goal == "开心"


@pytest.mark.asyncio
async def test_standalone_emoji_uses_deterministic_planner_without_provider() -> None:
    provider = FakePlannerProvider(TurnPlan(**_valid_plan_payload()))
    observability = PlannerObservability()
    service = PlannerService(provider=provider, observability=observability)
    planner_input = _planner_input(text="发个开心的表情").model_copy(
        update={
            "emoji": PlannerEmojiContext(
                enabled=True,
                available=True,
                explicit_request=True,
                standalone_request=True,
                goal="开心",
            )
        }
    )

    outcome = await service.plan(planner_input, runtime=_runtime(), turn_version=1)

    assert provider.inputs == []
    assert outcome.planned_turn.planner_used is False
    assert outcome.planned_turn.plan.reason_code is PlannerReasonCode.DETERMINISTIC_EFFECT_REQUEST
    assert outcome.planned_turn.plan.emoji.mode is EmojiReplyMode.EMOJI_ONLY
    assert outcome.planned_turn.plan.tool_mode is ToolMode.NONE
    assert outcome.planned_turn.plan.tool_selection.scope_ids == ()
    assert outcome.planned_turn.plan.memory_context.mode is MemoryContextMode.NONE
    snapshot = observability.snapshot()
    assert snapshot.deterministic_effects == 1
    assert snapshot.total_requests == 0


def test_planner_messages_use_the_shared_chat_message_shape() -> None:
    planner_input = _planner_input()

    payload = planner_input.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
        exclude_computed_fields=True,
    )

    assert payload["history_messages"] == [
        {
            "role": "user",
            "content": "[历史用户|QQ:1002]\n#100>earlier",
        }
    ]
    assert payload["current_message"] == {
        "role": "user",
        "content": "[测试用户|QQ:1001]\n#101>帮我看看",
    }
    assert "trusted_history_sender_user_ids" not in payload
    assert "trusted_history_event_ids" not in payload


def test_private_message_has_base_relevance_and_enters_planner() -> None:
    result = ReplyNecessityScorer().score(
        ReplyNecessityFeatures(scope_type=ScopeType.PRIVATE, text="在吗")
    )
    assert result.relevance_score > 0
    assert result.should_enter_planner


def test_mention_and_reply_to_yuki_are_strong_forced_signals() -> None:
    scorer = ReplyNecessityScorer()
    mention = scorer.score(
        ReplyNecessityFeatures(scope_type=ScopeType.GROUP, text="嗯", mentions_bot=True)
    )
    reply = scorer.score(
        ReplyNecessityFeatures(scope_type=ScopeType.GROUP, text="嗯", reply_target_is_bot=True)
    )
    assert mention.should_enter_planner and mention.relevance_score >= 50
    assert reply.should_enter_planner and reply.relevance_score >= 50


def test_low_value_reaction_scores_below_question_request_and_opinion() -> None:
    scorer = ReplyNecessityScorer()

    def score(text: str) -> int:
        return scorer.score(ReplyNecessityFeatures(scope_type=ScopeType.GROUP, text=text)).score

    low = score("哈哈")
    assert score("这是什么？") > low
    assert score("请帮我查一下资料") > low
    assert score("你觉得这个方案怎么样？") > low


def test_pressure_presence_and_fast_activity_are_distinct_components() -> None:
    scorer = ReplyNecessityScorer()
    accumulated = scorer.score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            pending_message_count=8,
            average_human_interval_seconds=60,
        )
    )
    overactive = scorer.score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            recent_bot_messages=8,
            recent_total_messages=10,
            seconds_since_last_bot_message=5,
        )
    )
    fast = scorer.score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="继续聊聊",
            average_human_interval_seconds=1,
        )
    )
    assert accumulated.pressure_score > 0
    assert overactive.presence_penalty > 0
    assert fast.activity_penalty > 0


def test_idle_compensation_requires_a_real_new_message() -> None:
    result = ReplyNecessityScorer().score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="",
            idle_seconds=3600,
            new_message_count=0,
        )
    )
    assert result.score == 0
    assert not result.should_enter_planner
    assert result.pressure_score == 0


def test_relationship_and_plugin_adjustments_are_bounded() -> None:
    now = datetime.now(UTC)
    signals = tuple(
        PlannerSignal(
            source_plugin_id=f"plugin-{index}",
            score_delta=10.0,
            reason_code="relevant",
            confidence=1.0,
            expires_at=now + timedelta(minutes=1),
        )
        for index in range(3)
    )
    result = ReplyNecessityScorer().score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="普通内容",
            relationship_adjustment=999,
            plugin_signals=signals,
            now=now,
        )
    )
    assert result.relationship_adjustment == 5
    assert result.plugin_adjustment == 15


def test_same_plugin_and_expired_signals_cannot_bypass_caps() -> None:
    now = datetime.now(UTC)
    signals = (
        PlannerSignal(
            source_plugin_id="same",
            score_delta=10.0,
            reason_code="one",
            confidence=1.0,
        ),
        PlannerSignal(
            source_plugin_id="same",
            score_delta=10.0,
            reason_code="two",
            confidence=1.0,
        ),
        PlannerSignal(
            source_plugin_id="expired",
            score_delta=10.0,
            reason_code="old",
            confidence=1.0,
            expires_at=now - timedelta(seconds=1),
        ),
    )
    result = ReplyNecessityScorer().score(
        ReplyNecessityFeatures(
            scope_type=ScopeType.GROUP,
            text="普通内容",
            plugin_signals=signals,
            now=now,
        )
    )
    assert result.plugin_adjustment == 10


def test_planner_applies_hot_natural_multi_target_without_affecting_structure() -> None:
    runtime = _runtime()
    runtime = replace(
        runtime,
        planner=replace(runtime.planner, preferred_messages=4),
        reply=replace(runtime.reply, plan_hard_max_messages=20),
    )
    planner_input = _planner_input(text="今天过得怎么样")
    natural = PlannerService._constrain_business_rules(
        TurnPlan(**_valid_plan_payload(delivery_mode="natural_multi", desired_messages=1)),
        planner_input,
        runtime,
        administrator_request=False,
    )
    structured = PlannerService._constrain_business_rules(
        TurnPlan(**_valid_plan_payload(delivery_mode="structured", desired_messages=7)),
        planner_input,
        runtime,
        administrator_request=False,
    )
    explicit = PlannerService._constrain_business_rules(
        TurnPlan(**_valid_plan_payload(delivery_mode="single", desired_messages=1)),
        _planner_input(text="你尝试多发几条消息"),
        runtime,
        administrator_request=False,
    )
    assert natural.delivery_mode is DeliveryMode.NATURAL_MULTI
    assert natural.desired_messages == 4
    assert structured.delivery_mode is DeliveryMode.STRUCTURED
    assert structured.desired_messages == 7
    assert explicit.delivery_mode is DeliveryMode.NATURAL_MULTI
    assert explicit.desired_messages == 4


def test_planner_voice_language_is_bounded_by_the_active_profile() -> None:
    runtime = replace(_runtime(), speech=replace(_runtime().speech, enabled=True))
    planner_input = _planner_input().model_copy(
        update={
            "speech": PlannerSpeechContext(
                enabled=True,
                available=True,
                default_profile="roxy",
                available_styles=("neutral", "gentle"),
                available_languages=("zh", "jp"),
            )
        }
    )
    japanese = PlannerService._constrain_business_rules(
        TurnPlan(
            **_valid_plan_payload(
                voice=VoiceReplyPlan(
                    mode=VoiceMode.OPTIONAL,
                    style_hint="gentle",
                    language=SpeechLanguageHint.JP,
                )
            )
        ),
        planner_input,
        runtime,
        administrator_request=False,
    )
    unavailable = PlannerService._constrain_business_rules(
        TurnPlan(
            **_valid_plan_payload(
                voice=VoiceReplyPlan(
                    mode=VoiceMode.OPTIONAL,
                    style_hint="unknown",
                    language=SpeechLanguageHint.JP,
                )
            )
        ),
        planner_input.model_copy(
            update={
                "speech": planner_input.speech.model_copy(update={"available_languages": ("zh",)})
            }
        ),
        runtime,
        administrator_request=False,
    )

    assert japanese.voice.language.value == "jp"
    assert japanese.voice.style_hint == "gentle"
    assert unavailable.voice.language.value == "auto"
    assert unavailable.voice.style_hint == ""


def test_planner_owns_emoji_effect_and_closes_tools_for_exclusive_delivery() -> None:
    runtime = _runtime()
    available = PlannerEmojiContext(enabled=True, available=True)
    planner_input = _planner_input(text="发个表情").model_copy(update={"emoji": available})

    explicit = PlannerService._constrain_business_rules(
        TurnPlan(
            **_valid_plan_payload(
                emoji=EmojiReplyPlan(
                    intent=EmojiIntent.EXPLICIT_REQUEST,
                    mode=EmojiReplyMode.NONE,
                )
            )
        ),
        planner_input,
        runtime,
        administrator_request=False,
    )
    explicit_optional = PlannerService._constrain_business_rules(
        TurnPlan(
            **_valid_plan_payload(
                emoji=EmojiReplyPlan(
                    intent=EmojiIntent.EXPLICIT_REQUEST,
                    mode=EmojiReplyMode.OPTIONAL,
                    goal="轻松回应",
                )
            )
        ),
        planner_input,
        runtime,
        administrator_request=False,
    )
    exclusive = PlannerService._constrain_business_rules(
        TurnPlan(
            **_valid_plan_payload(
                emoji=EmojiReplyPlan(
                    intent=EmojiIntent.EXPLICIT_REQUEST,
                    mode=EmojiReplyMode.EMOJI_ONLY,
                    placement=EmojiPlacement.AFTER_TEXT,
                    goal="直接发一张",
                )
            )
        ),
        planner_input,
        runtime,
        administrator_request=False,
    )
    spontaneous_blocked = PlannerService._constrain_business_rules(
        TurnPlan(
            **_valid_plan_payload(
                emoji=EmojiReplyPlan(
                    intent=EmojiIntent.NEUTRAL,
                    mode=EmojiReplyMode.OPTIONAL,
                    goal="日常回应",
                )
            )
        ),
        planner_input.model_copy(
            update={"emoji": available.model_copy(update={"spontaneous_allowed": False})}
        ),
        runtime,
        administrator_request=False,
    )

    assert explicit.emoji.mode is EmojiReplyMode.PREFERRED
    assert explicit.emoji.goal == "发个表情"
    assert explicit_optional.emoji.mode is EmojiReplyMode.PREFERRED
    assert exclusive.emoji.mode is EmojiReplyMode.EMOJI_ONLY
    assert exclusive.emoji.placement is EmojiPlacement.ONLY
    assert exclusive.emoji.is_exclusive
    assert exclusive.tool_selection.mode is ToolMode.NONE
    assert exclusive.tool_selection.scope_ids == ()
    assert exclusive.memory_context.mode is MemoryContextMode.NONE
    assert exclusive.memory_context.reason_code is MemoryContextReasonCode.EFFECT_ONLY
    assert spontaneous_blocked.emoji.mode is EmojiReplyMode.NONE


def test_planner_memory_context_is_separate_and_semantic_mode_degrades_cleanly() -> None:
    planner_input = _planner_input(text="我以前提到的那个人是谁")
    model_plan = TurnPlan(
        **_valid_plan_payload(
            memory_context=MemoryContextPlan(
                mode=MemoryContextMode.HYBRID,
                reason_code=MemoryContextReasonCode.PERSON_REFERENCE,
                self_recall=True,
            )
        )
    )
    semantic_runtime = _runtime()
    lexical_runtime = replace(
        semantic_runtime,
        memory=replace(semantic_runtime.memory, semantic_enabled=False),
    )
    self_enabled_input = planner_input.model_copy(
        update={"memory": planner_input.memory.model_copy(update={"self_enabled": True})}
    )

    hybrid = PlannerService._constrain_business_rules(
        model_plan,
        planner_input,
        semantic_runtime,
        administrator_request=False,
    )
    lexical = PlannerService._constrain_business_rules(
        model_plan,
        planner_input,
        lexical_runtime,
        administrator_request=False,
    )
    self_enabled = PlannerService._constrain_business_rules(
        model_plan,
        self_enabled_input,
        replace(
            semantic_runtime,
            memory=replace(semantic_runtime.memory, self_enabled=True),
        ),
        administrator_request=False,
    )

    assert hybrid.memory_context.mode is MemoryContextMode.HYBRID
    assert lexical.memory_context.mode is MemoryContextMode.LEXICAL
    assert lexical.memory_context.reason_code is MemoryContextReasonCode.PERSON_REFERENCE
    assert hybrid.memory_context.self_recall is False
    assert self_enabled.memory_context.self_recall is True


def test_planner_semantic_voice_intent_is_enforced_without_keyword_matching() -> None:
    runtime = replace(
        _runtime(),
        speech=replace(_runtime().speech, enabled=True, default_mode="optional"),
    )
    speech = PlannerSpeechContext(
        enabled=True,
        available=True,
        default_profile="roxy",
        available_styles=("neutral", "gentle"),
        available_languages=("zh", "jp"),
    )

    def constrained(voice: VoiceReplyPlan, *, spontaneous_allowed: bool = True) -> TurnPlan:
        planner_input = _planner_input(text="任意自然语言，不由后端匹配关键词").model_copy(
            update={
                "speech": speech.model_copy(update={"spontaneous_allowed": spontaneous_allowed})
            }
        )
        return PlannerService._constrain_business_rules(
            TurnPlan(**_valid_plan_payload(voice=voice)),
            planner_input,
            runtime,
            administrator_request=False,
        )

    explicit = constrained(
        VoiceReplyPlan(
            mode=VoiceMode.TEXT,
            intent=VoiceIntent.EXPLICIT_REQUEST,
            agent_tool=VoiceAgentToolPolicy.REQUIRED,
        )
    )
    opt_out = constrained(
        VoiceReplyPlan(
            mode=VoiceMode.TEXT_AND_VOICE,
            intent=VoiceIntent.EXPLICIT_OPT_OUT,
            preference_change=VoicePreferenceChange(
                mode=VoicePreferenceMode.TEXT_ONLY,
                duration=VoicePreferenceDuration.PERSISTENT,
            ),
        )
    )
    neutral = constrained(VoiceReplyPlan(mode=VoiceMode.OPTIONAL))
    cadence_blocked = constrained(
        VoiceReplyPlan(mode=VoiceMode.VOICE),
        spontaneous_allowed=False,
    )

    assert explicit.voice.mode is VoiceMode.VOICE
    assert explicit.voice.agent_tool is VoiceAgentToolPolicy.REQUIRED
    assert opt_out.voice.mode is VoiceMode.TEXT
    assert opt_out.voice.preference_change is not None
    assert neutral.voice.mode is VoiceMode.VOICE
    assert neutral.voice.agent_tool is VoiceAgentToolPolicy.FORBIDDEN
    assert cadence_blocked.voice.mode is VoiceMode.TEXT


def test_unavailable_speech_cannot_smuggle_a_neutral_persistent_preference() -> None:
    planner_input = _planner_input().model_copy(
        update={
            "speech": PlannerSpeechContext(enabled=False, available=False),
        }
    )
    model_plan = TurnPlan(
        **_valid_plan_payload(
            voice=VoiceReplyPlan(
                mode=VoiceMode.VOICE,
                preference_change=VoicePreferenceChange(
                    mode=VoicePreferenceMode.PREFER_VOICE,
                    duration=VoicePreferenceDuration.PERSISTENT,
                ),
            )
        )
    )

    constrained = PlannerService._constrain_business_rules(
        model_plan,
        planner_input,
        _runtime(),
        administrator_request=False,
    )

    assert constrained.voice.mode is VoiceMode.TEXT
    assert constrained.voice.agent_tool is VoiceAgentToolPolicy.FORBIDDEN
    assert constrained.voice.preference_change is None


def test_agent_speech_runtime_policy_contains_no_internal_transport_details() -> None:
    source = inspect.getsource(PromptComposer)
    assert "/run/yuki-speech" not in source
    assert "8080" not in source and "6099" not in source


def test_main_agent_plan_projection_excludes_planner_delivery_constraints() -> None:
    plan = TurnPlan(
        **_valid_plan_payload(
            delivery_mode="natural_multi",
            desired_messages=4,
        )
    )
    planned_turn = cast(PlannedTurn, SimpleNamespace(plan=plan))

    contribution = PromptComposer._plan_contribution(planned_turn)

    assert isinstance(contribution.payload, dict)
    assert contribution.payload["decision"] == "reply"
    assert contribution.payload["intent"] == plan.intent
    assert "tools" in contribution.payload
    assert "delivery" not in contribution.payload
    assert "messages" not in contribution.payload


@pytest.mark.parametrize(
    "planner_input",
    (
        _planner_input(scope=ScopeType.PRIVATE, text="随便聊聊"),
        _planner_input(scope=ScopeType.GROUP, text="在吗", mentions_bot=True),
        _planner_input(scope=ScopeType.GROUP, text="接着说", reply_target_is_bot=True),
    ),
)
def test_explicit_turns_cannot_be_silenced_or_delayed_by_planner(
    planner_input: PlannerInput,
) -> None:
    runtime = _runtime()
    model_plan = TurnPlan(
        **_valid_plan_payload(
            decision="silent",
            wait_seconds=30,
            reason_code="low_relevance",
        )
    )
    constrained = PlannerService._constrain_business_rules(
        model_plan,
        planner_input,
        runtime,
        administrator_request=False,
    )

    assert constrained.decision is PlannerDecision.REPLY
    assert constrained.wait_seconds == 0


def test_disabled_wait_budget_does_not_trigger_immediate_replanning() -> None:
    runtime = replace(
        _runtime(),
        planner=replace(_runtime().planner, max_wait_seconds=0),
    )
    planner_input = _planner_input(scope=ScopeType.GROUP, text="继续观察")
    model_plan = TurnPlan(
        **_valid_plan_payload(
            decision="wait",
            wait_seconds=30,
            reason_code="wait_for_more_context",
        )
    )

    constrained = PlannerService._constrain_business_rules(
        model_plan,
        planner_input,
        runtime,
        administrator_request=False,
    )

    assert constrained.decision is PlannerDecision.SILENT
    assert constrained.wait_seconds == 0


def test_plan_validation_narrows_limits_and_unknown_event_bindings() -> None:
    planner_input = _planner_input(visual=True)
    payload = _valid_plan_payload(
        decision="wait",
        target_user_ids=["unknown", "1002", "1002", "1001"],
        reply_to_event_id=999,
        desired_messages=19,
        wait_seconds=250,
        tool_mode="inherit",
    )
    plan = constrain_turn_plan(
        payload,
        planner_input,
        hard_max_messages=4,
        max_wait_seconds=30,
    )
    assert plan.target_user_ids == ("1002", "1001")
    assert plan.reply_to_event_id is None
    assert plan.desired_messages == 4
    assert plan.wait_seconds == 30

    unknown_reply = constrain_turn_plan(
        _valid_plan_payload(reply_to_event_id=999),
        _planner_input(scope=ScopeType.GROUP),
    )
    assert unknown_reply.reply_to_event_id is None


def test_reply_target_is_plain_by_default_but_preserves_intentional_quotes() -> None:
    private_input = _planner_input(scope=ScopeType.PRIVATE)
    current_private = constrain_turn_plan(
        _valid_plan_payload(reply_to_event_id=101),
        private_input,
    )
    older_private = constrain_turn_plan(
        _valid_plan_payload(reply_to_event_id=100),
        private_input,
    )
    current_group = constrain_turn_plan(
        _valid_plan_payload(reply_to_event_id=101),
        _planner_input(scope=ScopeType.GROUP),
    )

    assert current_private.reply_to_event_id is None
    assert older_private.reply_to_event_id == 100
    assert current_group.reply_to_event_id == 101


def test_plan_parser_rejects_unknown_fields_and_permission_modes() -> None:
    planner_input = _planner_input()
    with pytest.raises(PlannerResponseError):
        constrain_turn_plan(_valid_plan_payload(root=True), planner_input)
    with pytest.raises(PlannerResponseError):
        constrain_turn_plan(
            _valid_plan_payload(tool_selection={"mode": "write_all"}),
            planner_input,
        )


def test_dynamic_scopes_are_authoritative_and_legacy_groups_remain_compatible() -> None:
    planner_input = _planner_input().model_copy(
        update={
            "available_tool_scopes": (
                ToolScopeSummary(
                    scope_id="mcp.music",
                    parent="mcp",
                    display_name="Music",
                    description="music tools",
                    tool_count=2,
                    provider_ids=("mcp.music",),
                ),
                ToolScopeSummary(
                    scope_id="automation",
                    display_name="Automation",
                    tool_count=3,
                    provider_ids=("automation",),
                ),
            ),
            "available_tool_categories": (),
        }
    )
    valid = constrain_turn_plan(
        _valid_plan_payload(
            tool_selection={
                "mode": "inherit",
                "scopes": ["mcp.music", "automation"],
            }
        ),
        planner_input,
    )
    assert valid.tool_selection.scope_ids == ("mcp.music", "automation")

    memory_scopes_ignored = constrain_turn_plan(
        _valid_plan_payload(
            tool_selection={
                "mode": "inherit",
                "scopes": ["memory", "memory.read", "memory_change", "mcp.music"],
            }
        ),
        planner_input,
    )
    assert memory_scopes_ignored.tool_selection.scope_ids == ("mcp.music",)

    with pytest.raises(PlannerResponseError, match="unknown tool scopes"):
        constrain_turn_plan(
            _valid_plan_payload(tool_selection={"mode": "inherit", "scopes": ["mcp.unknown"]}),
            planner_input,
        )

    legacy = constrain_turn_plan(
        _valid_plan_payload(tool_selection={"mode": "inherit", "groups": ["automation"]}),
        planner_input,
    )
    assert legacy.tool_selection.scope_ids == ("automation",)
    assert legacy.tool_selection.groups == ()


@pytest.mark.asyncio
async def test_llm_planner_is_tool_free_non_thinking_and_uses_separate_model() -> None:
    payload = _valid_plan_payload()
    llm = FakeLLMProvider(lambda _request: json.dumps(payload))
    provider = LLMPlannerProvider(llm, model="planner-model")
    plan = await provider.plan(_planner_input(), runtime=_runtime())
    request = llm.requests[0]
    assert plan.decision is PlannerDecision.REPLY
    assert request.model == "planner-model"
    assert request.temperature == 0.1
    assert request.max_output_tokens == 512
    assert request.thinking_enabled is False
    assert request.tools == ()
    assert request.tool_choice is None
    assert "reply_to_event_id 默认必须为 null" in (request.messages[0].content or "")


@pytest.mark.asyncio
async def test_llm_planner_uses_configured_bot_name_in_its_instruction() -> None:
    llm = FakeLLMProvider(lambda _request: json.dumps(_valid_plan_payload()))
    provider = LLMPlannerProvider(llm, model="planner-model", bot_display_name="Mika")

    await provider.plan(_planner_input(), runtime=_runtime())

    instruction = llm.requests[0].messages[0].content or ""
    assert "明确询问 Mika 过去的偏好" in instruction
    assert "明确询问机器人自己" not in instruction


@pytest.mark.asyncio
async def test_llm_planner_materializes_sparse_output_with_backend_defaults() -> None:
    llm = FakeLLMProvider(
        lambda _request: json.dumps(
            {
                "decision": "reply",
                "confidence": 0.92,
                "reason_code": "direct_request",
                "delivery_mode": "single",
                "desired_messages": 1,
                "memory_context": {
                    "access": "automatic",
                    "mode": "lexical",
                    "purpose": "background",
                },
                "emoji": {"intent": "neutral", "mode": "none"},
                "voice": {"mode": "text", "intent": "neutral"},
            }
        )
    )
    provider = LLMPlannerProvider(llm, model="planner-model")

    plan = await provider.plan(_planner_input(), runtime=_runtime())

    assert plan.decision is PlannerDecision.REPLY
    assert plan.confidence == 0.92
    assert plan.intent == ""
    assert plan.delivery_mode is DeliveryMode.SINGLE
    assert plan.desired_messages == 1
    assert plan.reply_to_event_id is None
    assert plan.tool_mode is ToolMode.INHERIT
    assert plan.tool_selection.scope_ids == ()
    assert plan.tool_selection_explicit is False
    assert plan.memory_context.mode is MemoryContextMode.LEXICAL
    assert plan.emoji.mode is EmojiReplyMode.NONE
    assert plan.voice.mode is VoiceMode.TEXT


def test_sparse_planner_schema_requires_all_non_inferable_decisions() -> None:
    schema = PlannerModelOutput.model_json_schema()

    assert set(schema["required"]) == {
        "decision",
        "confidence",
        "reason_code",
        "delivery_mode",
        "memory_context",
        "emoji",
        "voice",
    }


def test_planner_schema_compaction_preserves_validation_and_halves_prose() -> None:
    schema = PlannerModelOutput.model_json_schema()
    compact = _compact_json_schema(schema)

    encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    compact_encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    assert len(compact_encoded) < len(encoded) * 0.6
    assert compact["required"] == schema["required"]
    assert not any(
        key in {"title", "description", "default"}
        for node in _walk_json_dicts(compact)
        for key in node
    )


def test_sparse_planner_derives_secondary_effect_defaults() -> None:
    output = PlannerModelOutput.model_validate(
        {
            "decision": "reply",
            "confidence": 0.98,
            "reason_code": "direct_request",
            "delivery_mode": "single",
            "desired_messages": 1,
            "memory_context": {
                "access": "none",
                "mode": "none",
                "purpose": "background",
            },
            "emoji": {"intent": "explicit_request", "mode": "emoji_only"},
            "voice": {"mode": "voice", "intent": "explicit_request"},
        }
    )

    plan = output.materialize()

    assert plan.emoji.placement is EmojiPlacement.ONLY
    assert plan.voice.agent_tool is VoiceAgentToolPolicy.REQUIRED
    assert plan.voice.language is SpeechLanguageHint.AUTO


def test_sparse_planner_preserves_explicit_empty_tool_selection() -> None:
    output = PlannerModelOutput.model_validate(
        {
            "decision": "reply",
            "confidence": 0.98,
            "reason_code": "direct_request",
            "delivery_mode": "single",
            "desired_messages": 1,
            "tool_selection": {"mode": "inherit", "scopes": []},
            "memory_context": {
                "access": "automatic",
                "mode": "lexical",
                "purpose": "background",
            },
            "emoji": {"intent": "neutral", "mode": "none"},
            "voice": {"mode": "text", "intent": "neutral"},
        }
    )

    plan = output.materialize()

    assert plan.tool_selection.scope_ids == ()
    assert plan.tool_selection_explicit is True


@pytest.mark.asyncio
async def test_runtime_planner_limits_narrow_invalid_plan_without_losing_intent() -> None:
    payload = _valid_plan_payload(
        decision="wait",
        desired_messages=9,
        wait_seconds=50,
    )
    llm = FakeLLMProvider(lambda _request: json.dumps(payload))
    runtime = _runtime()
    runtime = replace(
        runtime,
        planner=replace(
            runtime.planner,
            temperature=0.25,
            max_output_tokens=333,
            timeout_seconds=4,
            max_wait_seconds=12,
        ),
        reply=replace(runtime.reply, plan_hard_max_messages=3),
    )
    provider = LLMPlannerProvider(
        llm,
        model="constructor-fallback",
        temperature=0.9,
        max_output_tokens=999,
        timeout_seconds=30,
        hard_max_messages=8,
        max_wait_seconds=40,
        fallback_on_error=False,
    )
    plan = await provider.plan(_planner_input(), runtime=runtime)
    assert plan.decision is PlannerDecision.WAIT
    assert plan.desired_messages == 1
    assert plan.wait_seconds == 12
    request = llm.requests[0]
    assert request.model == "constructor-fallback"
    assert request.temperature == 0.25
    assert request.max_output_tokens == 333


@pytest.mark.asyncio
async def test_invalid_planner_json_is_not_retried_and_falls_back_safely() -> None:
    llm = FakeLLMProvider(lambda _request: "not-json")
    provider = LLMPlannerProvider(llm)
    plan = await provider.plan(_planner_input(), runtime=_runtime())
    assert len(llm.requests) == 1
    assert plan.decision is PlannerDecision.REPLY
    assert plan.reason_code is PlannerReasonCode.PLANNER_INVALID_RESPONSE_FALLBACK


@pytest.mark.asyncio
async def test_planner_provider_failure_falls_back_without_tools() -> None:
    def unavailable(_request: object) -> str:
        raise LLMUnavailableError("provider unavailable")

    llm = FakeLLMProvider(unavailable)
    provider = LLMPlannerProvider(llm)
    plan = await provider.plan(_planner_input(), runtime=_runtime())
    assert len(llm.requests) == 1
    assert plan.reason_code is PlannerReasonCode.PLANNER_PROVIDER_ERROR_FALLBACK
    assert plan.tool_mode is ToolMode.NONE
    assert plan.tool_selection.scope_ids == ()
    assert plan.desired_messages == 1


@pytest.mark.asyncio
async def test_planner_timeout_has_distinct_narrow_fallback() -> None:
    llm = FakeLLMProvider(
        lambda _request: json.dumps(_valid_plan_payload()),
        delay_seconds=0.1,
    )
    provider = LLMPlannerProvider(llm)
    runtime = replace(
        _runtime(),
        planner=replace(_runtime().planner, timeout_seconds=0.001),
    )
    plan = await provider.plan(_planner_input(), runtime=runtime)
    assert plan.reason_code is PlannerReasonCode.PLANNER_TIMEOUT_FALLBACK
    assert plan.tool_mode is ToolMode.NONE
    assert plan.tool_selection.scope_ids == ()
    assert plan.desired_messages == 1


@pytest.mark.asyncio
async def test_admitted_autonomous_group_failure_falls_back_to_reply() -> None:
    llm = FakeLLMProvider(lambda _request: "not-json")
    provider = LLMPlannerProvider(llm)
    planner_input = _planner_input(
        scope=ScopeType.GROUP,
        origin=TurnOrigin.AUTONOMOUS_GROUP,
        text="Yuki，你觉得呢？",
    )
    planner_input = planner_input.model_copy(
        update={
            "necessity": planner_input.necessity.model_copy(update={"should_enter_planner": True})
        }
    )
    plan = await provider.plan(planner_input, runtime=_runtime())
    assert plan.decision is PlannerDecision.REPLY
    assert plan.tool_mode is ToolMode.NONE
    assert plan.tool_selection.scope_ids == ()


@pytest.mark.asyncio
async def test_cancellation_event_interrupts_an_active_llm_planner() -> None:
    llm = FakeLLMProvider(lambda _request: json.dumps(_valid_plan_payload()), delay_seconds=5)
    provider = LLMPlannerProvider(llm, timeout_seconds=10)
    cancellation = asyncio.Event()
    task = asyncio.create_task(
        provider.plan(_planner_input(), runtime=_runtime(), cancellation=cancellation)
    )
    for _ in range(100):
        if llm.requests:
            break
        await asyncio.sleep(0.001)
    cancellation.set()
    with pytest.raises(PlannerInterruptedError):
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_fake_planner_obeys_the_same_cancellation_event() -> None:
    provider = FakePlannerProvider(delay_seconds=5)
    cancellation = asyncio.Event()
    task = asyncio.create_task(
        provider.plan(_planner_input(), runtime=_runtime(), cancellation=cancellation)
    )
    await asyncio.sleep(0)
    cancellation.set()
    with pytest.raises(PlannerInterruptedError):
        await asyncio.wait_for(task, timeout=1)


def test_observability_tracks_active_fallback_and_hashes_identifiers() -> None:
    metrics = PlannerObservability()
    planner_input = _planner_input()
    with patch("qq_ai_bot.planner.observability.logger.info") as log_info:
        token = metrics.request_started(
            conversation_key=planner_input.conversation_key,
            sender_user_id=planner_input.current_sender_user_id,
            group_id=planner_input.current_group_id,
        )
        assert metrics.snapshot().active_requests == 1
        plan = TurnPlan(
            decision=PlannerDecision.REPLY,
            intent="reply",
            delivery_mode=DeliveryMode.SINGLE,
            desired_messages=1,
            tool_mode=ToolMode.NONE,
            wait_seconds=0.0,
            confidence=0.0,
            reason_code=PlannerReasonCode.PLANNER_FALLBACK,
        )
        metrics.request_finished(token, plan=plan, latency_seconds=0.2, fallback=True)
    rendered_logs = "\n".join(
        str(call.args[0]) % tuple(call.args[1:]) for call in log_info.call_args_list
    )
    snapshot = metrics.snapshot()
    assert snapshot.active_requests == 0
    assert snapshot.total_requests == 1
    assert snapshot.successful_plans == 1
    assert snapshot.fallback_plans == 1
    assert snapshot.last_decision is PlannerDecision.REPLY
    assert "tool_mode=none" in rendered_logs
    assert "planner_scope_source=explicit" in rendered_logs
    assert "planner_scopes=none" in rendered_logs
    assert "private:1001" not in rendered_logs
    assert "sender_user_id=1001" not in rendered_logs


def test_observability_records_inherited_tool_scopes() -> None:
    metrics = PlannerObservability()
    with patch("qq_ai_bot.planner.observability.logger.info") as log_info:
        token = metrics.request_started(
            conversation_key="private:1001",
            sender_user_id="1001",
            group_id=None,
        )
        plan = TurnPlan(
            decision=PlannerDecision.REPLY,
            delivery_mode=DeliveryMode.SINGLE,
            desired_messages=1,
            confidence=1.0,
            reason_code=PlannerReasonCode.DIRECT_REQUEST,
        )

        metrics.request_finished(token, plan=plan, latency_seconds=0.1)
    rendered_logs = "\n".join(
        str(call.args[0]) % tuple(call.args[1:]) for call in log_info.call_args_list
    )

    assert "planner_scope_source=inherited" in rendered_logs
    assert "planner_scopes=backend_authorized" in rendered_logs
    assert "private:1001" not in rendered_logs
