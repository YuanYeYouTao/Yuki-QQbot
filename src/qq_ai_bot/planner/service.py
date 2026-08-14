"""Planner orchestration, deterministic fallback, and monotonic backend constraints."""

from __future__ import annotations

import time
from dataclasses import dataclass

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.emoji.models import EmojiIntent, EmojiPlacement, EmojiReplyMode
from qq_ai_bot.memory.enums import MemoryContextMode
from qq_ai_bot.planner.models import (
    DeliveryMode,
    MemoryContextReasonCode,
    PlannedTurn,
    PlannerDecision,
    PlannerInput,
    PlannerReasonCode,
    ToolMode,
    ToolSelection,
    TurnPlan,
)
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.provider import (
    PlannerProvider,
    deterministic_effect_plan,
    deterministic_fallback_plan,
    normalize_reply_target,
)
from qq_ai_bot.planner.repository import PlannerRepository
from qq_ai_bot.services.plugin_events import (
    LifecycleEventPublisher,
    publish_notification,
)
from qq_ai_bot.speech.models import (
    SpeechLanguageHint,
    VoiceAgentToolPolicy,
    VoiceIntent,
    VoiceMode,
    VoicePreferenceDuration,
    VoicePreferenceMode,
)
from yuki_plugin_sdk.events import EventName

_MULTI_MESSAGE_REQUESTS = (
    "多发几条",
    "发多条",
    "多条消息",
    "分几条",
    "拆成几条",
    "分开发",
    "一句一条",
)


@dataclass(frozen=True, slots=True)
class PlannerOutcome:
    planned_turn: PlannedTurn
    run_id: int | None = None


class PlannerService:
    """Make Planner the single decision boundary before the normal Yuki Agent."""

    def __init__(
        self,
        *,
        provider: PlannerProvider,
        observability: PlannerObservability,
        repository: PlannerRepository | None = None,
        event_publisher: LifecycleEventPublisher | None = None,
    ) -> None:
        self._provider = provider
        self._observability = observability
        self._repository = repository
        self._event_publisher = event_publisher

    @property
    def observability(self) -> PlannerObservability:
        return self._observability

    @property
    def repository(self) -> PlannerRepository | None:
        return self._repository

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        """Attach the host notification bus without coupling Planner to PluginHost."""

        self._event_publisher = publisher

    async def plan(
        self,
        planner_input: PlannerInput,
        *,
        runtime: RuntimeConfigSnapshot,
        turn_version: int,
        administrator_request: bool = False,
    ) -> PlannerOutcome:
        started = time.perf_counter()
        self._observability.record_necessity(
            planner_input.necessity,
            conversation_key=planner_input.conversation_key,
        )
        enabled_for_turn = (
            runtime.planner.group_enabled
            if planner_input.origin is TurnOrigin.AUTONOMOUS_GROUP
            else runtime.planner.direct_enabled
        )
        deterministic_effect = bool(
            planner_input.emoji.available
            and planner_input.emoji.explicit_request
            and planner_input.emoji.standalone_request
        )
        should_call = (
            enabled_for_turn
            and planner_input.necessity.should_enter_planner
            and not deterministic_effect
        )
        planner_model = (
            str(getattr(self._provider, "model_name", "") or runtime.llm.model)
            if should_call
            else ""
        )
        run_id = (
            await self._begin_run(
                planner_input,
                planner_used=should_call,
                planner_model=planner_model,
            )
            if runtime.planner.record_runs
            else None
        )
        fallback_used = False
        if deterministic_effect:
            plan = deterministic_effect_plan(planner_input)
            self._observability.record_deterministic_effect(
                conversation_key=planner_input.conversation_key
            )
        elif not planner_input.necessity.should_enter_planner:
            plan = self._silent_gate_plan()
        elif not enabled_for_turn:
            plan = deterministic_fallback_plan(planner_input)
            fallback_used = True
        else:
            plan = await self._provider.plan(planner_input, runtime=runtime)
            fallback_used = plan.reason_code in {
                PlannerReasonCode.PLANNER_FALLBACK,
                PlannerReasonCode.PLANNER_TIMEOUT_FALLBACK,
                PlannerReasonCode.PLANNER_INVALID_RESPONSE_FALLBACK,
                PlannerReasonCode.PLANNER_PROVIDER_ERROR_FALLBACK,
            }
        plan = self._constrain_business_rules(
            plan,
            planner_input,
            runtime,
            administrator_request=administrator_request,
        )
        latency = time.perf_counter() - started
        planned = PlannedTurn(
            plan=plan,
            necessity=planner_input.necessity,
            planner_model=planner_model,
            planner_latency_seconds=latency,
            planner_used=should_call,
            fallback_used=fallback_used,
            turn_version=turn_version,
        )
        await self._finish_run(run_id, planned, planner_input)
        await publish_notification(
            self._event_publisher,
            EventName.PLANNER_PLANNED,
            {
                "trigger_message_id": planner_input.trigger_message_id,
                "scope_type": planner_input.scope_type.value,
                "origin": planner_input.origin.value,
                "decision": plan.decision.value,
                "reason_code": plan.reason_code.value,
                "delivery_mode": plan.delivery_mode.value,
                "desired_messages": plan.desired_messages,
                "tool_mode": plan.tool_mode.value,
                "memory_context_mode": plan.memory_context.mode.value,
                "memory_context_reason": plan.memory_context.reason_code.value,
                "memory_self_recall": plan.memory_context.self_recall,
                "voice_mode": plan.voice.mode.value,
                "voice_intent": plan.voice.intent.value,
                "voice_tool_policy": plan.voice.agent_tool.value,
                "confidence": plan.confidence,
                "planner_used": should_call,
                "fallback_used": fallback_used,
                "latency_milliseconds": round(latency * 1000),
                "turn_version": turn_version,
            },
        )
        return PlannerOutcome(planned, run_id)

    @staticmethod
    def _silent_gate_plan() -> TurnPlan:
        return TurnPlan(
            decision=PlannerDecision.SILENT,
            intent="回复必要性不足，本轮不打扰群聊",
            delivery_mode=DeliveryMode.CONCISE,
            desired_messages=1,
            tool_selection=ToolSelection(mode=ToolMode.NONE),
            confidence=1.0,
            reason_code=PlannerReasonCode.LOW_RELEVANCE,
        )

    @staticmethod
    def _constrain_business_rules(
        plan: TurnPlan,
        planner_input: PlannerInput,
        runtime: RuntimeConfigSnapshot,
        *,
        administrator_request: bool,
    ) -> TurnPlan:
        hard_max = runtime.reply.plan_hard_max_messages
        preferred = min(runtime.planner.preferred_messages, hard_max)
        fallback_reason = plan.reason_code in {
            PlannerReasonCode.PLANNER_FALLBACK,
            PlannerReasonCode.PLANNER_TIMEOUT_FALLBACK,
            PlannerReasonCode.PLANNER_INVALID_RESPONSE_FALLBACK,
            PlannerReasonCode.PLANNER_PROVIDER_ERROR_FALLBACK,
        }
        requested_multi = not fallback_reason and any(
            token in planner_input.current_text for token in _MULTI_MESSAGE_REQUESTS
        )
        delivery_mode = DeliveryMode.NATURAL_MULTI if requested_multi else plan.delivery_mode
        desired_messages = (
            preferred
            if delivery_mode is DeliveryMode.NATURAL_MULTI
            else min(plan.desired_messages, hard_max)
        )
        updates: dict[str, object] = {
            "delivery_mode": delivery_mode,
            "desired_messages": desired_messages,
            "reply_to_event_id": normalize_reply_target(
                plan.reply_to_event_id,
                planner_input,
            ),
            "wait_seconds": min(plan.wait_seconds, runtime.planner.max_wait_seconds),
        }
        if fallback_reason:
            updates.update(
                delivery_mode=DeliveryMode.CONCISE,
                desired_messages=1,
                tool_selection=ToolSelection(mode=ToolMode.NONE, scopes=()),
            )
        memory_context = plan.memory_context
        if not planner_input.memory.self_enabled:
            memory_context = memory_context.model_copy(
                update={
                    "self_recall": False,
                    "subjects": tuple(
                        subject
                        for subject in memory_context.subjects
                        if subject.value != "current_self"
                    ),
                }
            )
        if memory_context.mode is MemoryContextMode.HYBRID and not runtime.memory.semantic_enabled:
            memory_context = memory_context.model_copy(update={"mode": MemoryContextMode.LEXICAL})
        updates["memory_context"] = memory_context
        emoji_plan = plan.emoji
        if not planner_input.emoji.available:
            emoji_plan = emoji_plan.model_copy(
                update={
                    "mode": EmojiReplyMode.NONE,
                    "placement": EmojiPlacement.AFTER_TEXT,
                    "goal": "",
                    "emotion": "",
                }
            )
        elif emoji_plan.intent is EmojiIntent.EXPLICIT_REQUEST:
            emoji_updates: dict[str, object] = {}
            if emoji_plan.mode in {EmojiReplyMode.NONE, EmojiReplyMode.OPTIONAL}:
                emoji_updates["mode"] = EmojiReplyMode.PREFERRED
            if emoji_plan.placement is EmojiPlacement.ONLY:
                emoji_updates["mode"] = EmojiReplyMode.EMOJI_ONLY
            if not emoji_plan.goal:
                emoji_updates["goal"] = planner_input.current_text[:300]
            if emoji_updates:
                emoji_plan = emoji_plan.model_copy(update=emoji_updates)
        elif not planner_input.emoji.spontaneous_allowed:
            emoji_plan = emoji_plan.model_copy(
                update={
                    "mode": EmojiReplyMode.NONE,
                    "placement": EmojiPlacement.AFTER_TEXT,
                    "goal": "",
                    "emotion": "",
                }
            )
        if planner_input.emoji.available and emoji_plan.is_exclusive:
            emoji_plan = emoji_plan.model_copy(
                update={
                    "mode": EmojiReplyMode.EMOJI_ONLY,
                    "placement": EmojiPlacement.ONLY,
                }
            )
            # An effect-only reply is fully executable by the delivery layer.
            # No Agent tool scope or memory context may be attached to the same turn.
            updates["tool_selection"] = ToolSelection(mode=ToolMode.NONE)
            updates["memory_context"] = plan.memory_context.model_copy(
                update={
                    "mode": MemoryContextMode.NONE,
                    "reason_code": MemoryContextReasonCode.EFFECT_ONLY,
                    "self_recall": False,
                    "subjects": (),
                }
            )
        updates["emoji"] = emoji_plan
        speech_allowed = (
            runtime.speech.enabled
            and runtime.speech.planner_enabled
            and planner_input.speech.available
            and (
                runtime.speech.private_enabled
                if planner_input.scope_type is ScopeType.PRIVATE
                else runtime.speech.group_enabled
            )
        )
        voice_plan = plan.voice
        if not speech_allowed:
            voice_plan = voice_plan.model_copy(
                update={
                    "mode": VoiceMode.TEXT,
                    "agent_tool": VoiceAgentToolPolicy.FORBIDDEN,
                    "style_hint": "",
                    "language": SpeechLanguageHint.AUTO,
                }
            )
        else:
            if voice_plan.intent is VoiceIntent.EXPLICIT_OPT_OUT:
                voice_plan = voice_plan.model_copy(
                    update={
                        "mode": VoiceMode.TEXT,
                        "agent_tool": VoiceAgentToolPolicy.FORBIDDEN,
                    }
                )
            elif voice_plan.intent is VoiceIntent.EXPLICIT_REQUEST:
                voice_plan = voice_plan.model_copy(
                    update={
                        "mode": (
                            VoiceMode.VOICE
                            if voice_plan.mode in {VoiceMode.TEXT, VoiceMode.OPTIONAL}
                            else voice_plan.mode
                        ),
                        "agent_tool": VoiceAgentToolPolicy.REQUIRED,
                    }
                )
            else:
                neutral_updates: dict[str, object] = {
                    "agent_tool": VoiceAgentToolPolicy.FORBIDDEN,
                    "preference_change": None,
                }
                if (
                    planner_input.speech.preference_mode is VoicePreferenceMode.TEXT_ONLY
                    or not planner_input.speech.spontaneous_allowed
                ):
                    neutral_updates["mode"] = VoiceMode.TEXT
                elif voice_plan.mode is VoiceMode.OPTIONAL:
                    neutral_updates["mode"] = VoiceMode.VOICE
                voice_plan = voice_plan.model_copy(update=neutral_updates)
            voice_updates: dict[str, object] = {}
            if (
                voice_plan.style_hint
                and voice_plan.style_hint not in planner_input.speech.available_styles
            ):
                voice_updates["style_hint"] = ""
            if (
                voice_plan.language is not SpeechLanguageHint.AUTO
                and voice_plan.language.value not in planner_input.speech.available_languages
            ):
                voice_updates["language"] = SpeechLanguageHint.AUTO
            if voice_updates:
                voice_plan = voice_plan.model_copy(update=voice_updates)
        change = voice_plan.preference_change
        if change is not None and (
            planner_input.origin is TurnOrigin.AUTONOMOUS_GROUP
            or voice_plan.intent is VoiceIntent.NEUTRAL
        ):
            voice_plan = voice_plan.model_copy(update={"preference_change": None})
        elif change is not None and change.duration is VoicePreferenceDuration.TURN:
            # Turn-only semantics are already represented by mode and intent;
            # only persistent transitions are stored after planning.
            voice_plan = voice_plan.model_copy(update={"preference_change": None})
        updates["voice"] = voice_plan
        explicit = (
            planner_input.scope_type is ScopeType.PRIVATE
            or planner_input.mentions_bot
            or planner_input.reply_target_is_bot
        )
        if (
            plan.decision is PlannerDecision.WAIT
            and runtime.planner.max_wait_seconds <= 0
            and not explicit
        ):
            # A disabled wait budget means "decide now", not "re-plan now".
            # Turning the result silent avoids a second model request with no
            # intervening message while explicit/admin turns are forced to reply below.
            updates.update(
                decision=PlannerDecision.SILENT,
                wait_seconds=0.0,
            )
        text = planner_input.current_text
        looks_like_request = any(
            token in text
            for token in ("?", "？", "请", "帮我", "怎么", "为什么", "能不能", "改成", "设置")
        )
        if (
            explicit
            or administrator_request
            or (planner_input.scope_type is ScopeType.PRIVATE and looks_like_request)
        ):
            updates["decision"] = PlannerDecision.REPLY
            updates["wait_seconds"] = 0.0
        if (
            planner_input.origin is TurnOrigin.AUTONOMOUS_GROUP
            and plan.decision is PlannerDecision.REPLY
            and not fallback_reason
            and plan.confidence < runtime.planner.confidence_threshold
        ):
            updates.update(
                decision=PlannerDecision.SILENT,
                reason_code=PlannerReasonCode.LOW_RELEVANCE,
                wait_seconds=0.0,
            )
        if not explicit and planner_input.origin is TurnOrigin.AUTONOMOUS_GROUP:
            # The model may only decide after the deterministic gate has admitted it.
            if not planner_input.necessity.should_enter_planner:
                updates["decision"] = PlannerDecision.SILENT
        return plan.model_copy(update=updates)

    async def _begin_run(
        self,
        planner_input: PlannerInput,
        *,
        planner_used: bool,
        planner_model: str,
    ) -> int | None:
        if self._repository is None:
            return None
        row = await self._repository.begin(
            conversation_key=planner_input.conversation_key,
            trigger_message_id=planner_input.trigger_message_id,
            scope_type=planner_input.scope_type.value,
            origin=planner_input.origin.value,
            sender_user_id=planner_input.current_sender_user_id,
            group_id=planner_input.current_group_id,
            necessity_score=planner_input.necessity.score,
            necessity_reasons={"reasons": planner_input.necessity.reasons},
            gate_decision=("enter" if planner_input.necessity.should_enter_planner else "skip"),
            planner_used=planner_used,
            planner_model=planner_model,
        )
        return row.id

    async def _finish_run(
        self,
        run_id: int | None,
        planned: PlannedTurn,
        planner_input: PlannerInput,
    ) -> None:
        if self._repository is None or run_id is None:
            return
        plan = planned.plan
        await self._repository.finish(
            run_id,
            planner_decision=plan.decision.value,
            reason_code=plan.reason_code.value,
            delivery_mode=plan.delivery_mode.value,
            desired_messages=plan.desired_messages,
            tool_mode=plan.tool_mode.value,
            voice_mode=plan.voice.mode.value,
            voice_intent=plan.voice.intent.value,
            voice_tool_policy=plan.voice.agent_tool.value,
            voice_reason=plan.voice.reason,
            voice_preference_change=(
                plan.voice.preference_change.mode.value
                if plan.voice.preference_change is not None
                else None
            ),
            spontaneous_frequency=planner_input.speech.spontaneous_frequency,
            recent_voice_ratio=planner_input.speech.recent_spontaneous_voice_ratio,
            confidence=plan.confidence,
            latency_seconds=planned.planner_latency_seconds,
            fallback_used=planned.fallback_used,
            messages_planned=plan.desired_messages if plan.decision is PlannerDecision.REPLY else 0,
        )

    async def record_delivery(
        self,
        run_id: int | None,
        *,
        messages_sent: int,
        interrupted: bool = False,
        error_category: str | None = None,
    ) -> None:
        if self._repository is not None and run_id is not None:
            await self._repository.update_delivery(
                run_id,
                messages_sent=messages_sent,
                interrupted=interrupted,
                error_category=error_category,
            )
