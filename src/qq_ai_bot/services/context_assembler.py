"""Bounded person-centric context assembly for one normal chat Agent."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.history.errors import (
    FrontierInvariantError,
    HistoryJobConflictError,
)
from qq_ai_bot.conversation.history.models import (
    ConversationHistoryIdentity,
    ConversationHistorySummary,
)
from qq_ai_bot.conversation.history.renderer import HistorySummaryRenderer
from qq_ai_bot.conversation.history.repository import ConversationHistoryRepository
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    InboundMessage,
    SenderIdentity,
)
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.domain.relationships import RelationshipSnapshot
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.memory.attribution import MemoryExposure, MemoryExposureSource
from qq_ai_bot.memory.context import (
    MemoryContextService,
    retrieval_fact_context,
    self_retrieval_fact_context,
)
from qq_ai_bot.memory.enums import MemoryContextMode, MemoryTargetRole
from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryRetrievalResult
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    EventRecord,
    PeopleRepository,
    RelationshipRepository,
)
from qq_ai_bot.prompting import ContextBudgeter, ContextContribution
from qq_ai_bot.time.formatting import local_iso
from qq_ai_bot.time.models import TimeContext
from qq_ai_bot.time.service import TimeContextService

logger = logging.getLogger(__name__)
_HISTORY_WINDOW_STATE_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class ContextMetrics:
    """Non-sensitive size diagnostics for one assembled context."""

    metadata_characters: int
    history_characters: int
    history_messages: int
    current_message_characters: int
    raw_history_window_shifted: bool
    rollup_characters: int = 0
    rollup_mode: str | None = None
    covered_to: int | None = None


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """Trusted dynamic context and bounded chat history for one model request."""

    metadata_payload: dict[str, Any]
    history_messages: tuple[ChatMessage, ...]
    current_message: ChatMessage
    recent_delivery: tuple[dict[str, object], ...]
    current_time: TimeContext
    current_relationship: RelationshipSnapshot | None
    metrics: ContextMetrics
    visible_event_ids: frozenset[int] = frozenset()
    external_events: tuple[dict[str, object], ...] = ()
    memory_turn_id: str = ""
    injected_memory_ids: tuple[int, ...] = ()
    memory_exposures: tuple[MemoryExposure, ...] = ()
    memory_intent: MemoryQueryIntent | None = None
    prompt_cache_key: str = ""
    history_anchor_event_id: int | None = None
    session_text: str = ""
    conversation_summary: tuple[object, ...] | None = None


@dataclass(frozen=True, slots=True)
class _BoundedMessages:
    """A bounded history window plus the separately preserved current input."""

    history_messages: tuple[ChatMessage, ...]
    current_message: ChatMessage
    history_anchor_event_id: int | None
    raw_history_window_shifted: bool
    visible_event_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class _HistoryWindowSelection:
    """One stable history epoch selected between configurable watermarks."""

    messages: tuple[ChatMessage, ...]
    anchor_event_id: int | None
    shifted: bool
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _HistoryPromptWindow:
    """One consistent rollup snapshot used to compile SESSION plus uncovered raw."""

    recent: tuple[EventRecord, ...]
    session_text: str
    coverage_end: int
    revision: int
    frontier: tuple[ConversationHistorySummary, ...]
    rollup_mode: str | None


class ConversationHistoryCoverage(Protocol):
    async def ensure_extractive_coverage(
        self,
        record: EventRecord,
        *,
        rendered: tuple[tuple[int, tuple[int, ...], ChatMessage], ...],
        anchor_event_id: int | None,
        high_event_limit: int,
        high_character_limit: int,
        fallback_anchor_event_id: int | None,
    ) -> object | None: ...


class ContextAssembler:
    """Load and bound all person, group, relationship, and history context."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: EventLedgerRepository,
        people: PeopleRepository,
        memory_context: MemoryContextService,
        relationships: RelationshipRepository,
        time_service: TimeContextService,
        history_repository: ConversationHistoryRepository | None = None,
        history_coverage: ConversationHistoryCoverage | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._people = people
        self._memory_context = memory_context
        self._relationships = relationships
        self._time = time_service
        self._history_repository = history_repository
        self._history_coverage = history_coverage
        self._history_renderer = HistorySummaryRenderer()
        self._history_window_anchors: OrderedDict[str, int] = OrderedDict()

    async def assemble(
        self,
        *,
        inbound: InboundMessage,
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        content: str,
        runtime: RuntimeConfigSnapshot,
        memory_mode: MemoryContextMode = MemoryContextMode.LEXICAL,
        self_recall: bool = False,
        memory_intent: MemoryQueryIntent | None = None,
        requested_limit: int | None = None,
        turn_origin: str = "user_message",
        memory_retrieval: MemoryRetrievalResult | None = None,
        persist_memory_exposure: bool = True,
    ) -> AssembledContext:
        """Build one bounded snapshot without persisting model-only metadata."""

        reset = await self._ledger.context_reset(identity)
        snapshot = await self._load_history_snapshot(
            bot_user_id=inbound.bot_user_id,
            scope_type=inbound.scope_type,
            user_id=inbound.sender.user_id,
            group_id=inbound.group_id,
            reset=reset,
            event_limit=runtime.context.local_event_limit,
        )
        recent = snapshot.recent
        history_window_key = self._history_window_key(
            identity, reset, coverage_end=snapshot.coverage_end, revision=snapshot.revision
        )
        external_events = self._external_event_context(recent)
        if memory_retrieval is not None:
            retrieval = memory_retrieval
        else:
            retrieval = await self._memory_context.retrieve_for_turn(
                inbound=inbound,
                content=content,
                runtime=runtime,
                memory_mode=memory_mode,
                self_recall=self_recall,
                memory_intent=memory_intent,
                requested_limit=requested_limit,
            )
        hits_by_role = {
            block.target.role: block.hits
            for block in retrieval.blocks
            if block.target.role
            in {
                MemoryTargetRole.CURRENT_PERSON,
                MemoryTargetRole.CURRENT_SELF,
                MemoryTargetRole.CURRENT_PERSON_GROUP,
                MemoryTargetRole.CURRENT_GROUP,
            }
        }
        aliases = await self._people.aliases(inbound.sender.user_id)
        current_time = await self._time.current(inbound.sender.user_id)
        current_relationship = (
            await self._relationships.get_or_create(
                inbound.sender.user_id,
                initial_affection=runtime.relationship.initial_affection,
                initial_trust=runtime.relationship.initial_trust,
            )
            if self._settings.relationship_enabled
            else None
        )

        context: dict[str, Any] = {
            "current_person": {
                "user_id": inbound.sender.user_id,
                "nickname": profile.nickname,
                "display_name": profile.display_name,
                "aliases": list(aliases),
                "facts": [
                    retrieval_fact_context(
                        hit,
                        self._settings.default_timezone,
                        include_budget_metadata=True,
                    )
                    for hit in hits_by_role.get(MemoryTargetRole.CURRENT_PERSON, ())
                ],
                **(
                    {"relationship": self.relationship_json(current_relationship)}
                    if current_relationship is not None
                    else {}
                ),
            },
            "scene": {
                "type": inbound.scope_type.value,
                "group_id": inbound.group_id,
                "group_card": profile.group_card,
            },
        }
        if external_events:
            context["recent_external_events"] = list(external_events)
        self_hits = hits_by_role.get(MemoryTargetRole.CURRENT_SELF, ())
        if self_hits:
            context["current_self"] = {
                "facts": [
                    self_retrieval_fact_context(
                        hit,
                        self._settings.default_timezone,
                        include_budget_metadata=True,
                    )
                    for hit in self_hits
                ]
            }
        context["available_memory_subjects"] = await self._available_memory_subjects(
            inbound,
            profile,
        )

        if inbound.group_id is not None:
            context["current_person_in_group"] = {
                "user_id": inbound.sender.user_id,
                "group_id": inbound.group_id,
                "facts": [
                    retrieval_fact_context(
                        hit,
                        self._settings.default_timezone,
                        include_budget_metadata=True,
                    )
                    for hit in hits_by_role.get(MemoryTargetRole.CURRENT_PERSON_GROUP, ())
                ],
            }
            context["current_group"] = {
                "group_id": inbound.group_id,
                "facts": [
                    retrieval_fact_context(
                        hit,
                        self._settings.default_timezone,
                        include_budget_metadata=True,
                    )
                    for hit in hits_by_role.get(MemoryTargetRole.CURRENT_GROUP, ())
                ],
            }
            referenced: dict[str, dict[str, Any]] = {}
            for block in retrieval.blocks:
                target = block.target
                if (
                    target.role
                    not in {
                        MemoryTargetRole.REFERENCED_PERSON,
                        MemoryTargetRole.REFERENCED_PERSON_GROUP,
                    }
                    or target.subject_user_id is None
                ):
                    continue
                entry = referenced.setdefault(
                    target.subject_user_id,
                    {
                        "user_id": target.subject_user_id,
                        "group_id": inbound.group_id,
                        "person_facts": [],
                        "group_facts": [],
                    },
                )
                key = (
                    "person_facts"
                    if target.role is MemoryTargetRole.REFERENCED_PERSON
                    else "group_facts"
                )
                entry[key] = [
                    retrieval_fact_context(
                        hit,
                        self._settings.default_timezone,
                        include_budget_metadata=True,
                    )
                    for hit in block.hits
                ]
            if referenced:
                context["referenced_people"] = list(referenced.values())

        total_budget = self._settings.max_context_characters
        metadata_budget = max(
            1,
            int(total_budget * self._settings.context_metadata_budget_ratio),
        )
        metadata_payload, selected_fact_ids = self._fit_metadata(context, metadata_budget)
        memory_exposures = self._memory_exposures(retrieval, selected_fact_ids)
        recall_turn = None
        if persist_memory_exposure:
            await self._memory_context.mark_injected(retrieval, selected_fact_ids)
            recall_turn = await self._memory_context.record_recall(
                conversation_key=identity.key,
                trigger_message_id=inbound.message_id,
                origin=turn_origin,
                intent=memory_intent,
                result=retrieval,
                injected_fact_ids=selected_fact_ids,
                runtime=runtime,
            )
        metadata_json = json.dumps(
            metadata_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        remainder = max(0, total_budget - len(metadata_json))
        snapshot, recent, session_text = await self._ensure_coverage_before_shift(
            snapshot=snapshot,
            recent=recent,
            inbound=inbound,
            content=content,
            remainder=remainder,
            event_limit=runtime.context.local_event_limit,
            reset=reset,
            identity=identity,
        )
        history_window_key = self._history_window_key(
            identity, reset, coverage_end=snapshot.coverage_end, revision=snapshot.revision
        )
        history_budget = max(0, remainder - len(session_text))
        if snapshot.coverage_end > 0:
            history_budget = int(
                history_budget * self._settings.conversation_history_raw_tail_budget_ratio
            )
        bounded_messages = self._bounded_history(
            recent,
            inbound=inbound,
            content=content,
            bot_display_name=self._settings.bot_display_name,
            timezone=self._settings.default_timezone,
            character_budget=history_budget,
            event_limit=runtime.context.local_event_limit,
            low_watermark_ratio=self._settings.history_window_low_watermark_ratio,
            anchor_event_id=self._history_window_anchor(history_window_key),
            allow_shift=snapshot.coverage_end > 0,
        )
        self._remember_history_window_anchor(
            history_window_key,
            bounded_messages.history_anchor_event_id,
        )
        history_messages = bounded_messages.history_messages
        current_message = bounded_messages.current_message
        history_characters = sum(len(message.content or "") for message in history_messages)
        metrics = ContextMetrics(
            metadata_characters=len(metadata_json),
            history_characters=history_characters,
            history_messages=len(history_messages),
            current_message_characters=len(current_message.content or ""),
            raw_history_window_shifted=bounded_messages.raw_history_window_shifted,
            rollup_characters=len(session_text),
            rollup_mode=snapshot.rollup_mode,
            covered_to=snapshot.coverage_end or None,
        )
        logger.debug(
            "context_assembled metadata_characters=%d history_characters=%d "
            "history_messages=%d current_message_characters=%d "
            "raw_history_window_shifted=%s rollup_characters=%d",
            metrics.metadata_characters,
            metrics.history_characters,
            metrics.history_messages,
            metrics.current_message_characters,
            metrics.raw_history_window_shifted,
            metrics.rollup_characters,
        )
        return AssembledContext(
            metadata_payload=metadata_payload,
            history_messages=history_messages,
            current_message=current_message,
            recent_delivery=self._recent_delivery(recent, self._settings.default_timezone),
            current_time=current_time,
            current_relationship=current_relationship,
            metrics=metrics,
            visible_event_ids=bounded_messages.visible_event_ids,
            external_events=external_events,
            memory_turn_id=recall_turn.turn_id if recall_turn is not None else "",
            injected_memory_ids=selected_fact_ids,
            memory_exposures=memory_exposures,
            memory_intent=memory_intent,
            prompt_cache_key=history_window_key,
            history_anchor_event_id=bounded_messages.history_anchor_event_id,
            session_text=session_text,
            conversation_summary=snapshot.frontier or None,
        )

    async def assemble_external(
        self,
        *,
        event: EventRecord,
        authorization_user_id: str,
        runtime: RuntimeConfigSnapshot,
        agent_intent: str,
    ) -> AssembledContext:
        """Assemble a main-conversation turn without inventing a human speaker."""

        inbound = InboundMessage(
            message_id=event.platform_message_id,
            event_type="external_event",
            scope_type=event.scope_type,
            sender=SenderIdentity(user_id=authorization_user_id),
            text=event.content,
            bot_user_id=event.bot_user_id,
            group_id=event.group_id,
            received_at=event.occurred_at,
        )
        history_identity = (
            ConversationIdentity.group(event.group_id, authorization_user_id)
            if event.scope_type is ScopeType.GROUP and event.group_id is not None
            else ConversationIdentity.private(event.private_peer_user_id or authorization_user_id)
        )
        reset = await self._ledger.context_reset(history_identity)
        snapshot = await self._load_history_snapshot(
            bot_user_id=event.bot_user_id,
            scope_type=event.scope_type,
            user_id=event.private_peer_user_id or authorization_user_id,
            group_id=event.group_id,
            reset=reset,
            event_limit=runtime.context.local_event_limit,
        )
        recent = snapshot.recent
        history_window_key = self._history_window_key(
            history_identity, reset, coverage_end=snapshot.coverage_end, revision=snapshot.revision
        )
        retrieval = await self._memory_context.retrieve_for_turn(
            inbound=inbound,
            content=event.content,
            runtime=runtime,
            memory_mode=MemoryContextMode.LEXICAL,
            self_recall=True,
            neutral_ordering=True,
        )
        hits_by_role = {
            block.target.role: block.hits
            for block in retrieval.blocks
            if block.target.role in {MemoryTargetRole.CURRENT_SELF, MemoryTargetRole.CURRENT_GROUP}
        }
        context: dict[str, Any] = {
            "scene": {
                "type": event.scope_type.value,
                "group_id": event.group_id,
                "trigger": "external_event",
            }
        }
        group_hits = hits_by_role.get(MemoryTargetRole.CURRENT_GROUP, ())
        if event.group_id is not None:
            context["current_group"] = {
                "group_id": event.group_id,
                "facts": [
                    retrieval_fact_context(
                        hit,
                        self._settings.default_timezone,
                        include_budget_metadata=True,
                    )
                    for hit in group_hits
                ],
            }
        self_hits = hits_by_role.get(MemoryTargetRole.CURRENT_SELF, ())
        if self_hits:
            context["current_self"] = {
                "facts": [
                    self_retrieval_fact_context(
                        hit,
                        self._settings.default_timezone,
                        include_budget_metadata=True,
                    )
                    for hit in self_hits
                ]
            }
        external_events = self._external_event_context(recent)
        if external_events:
            context["recent_external_events"] = list(external_events)
        metadata_payload, _selected_fact_ids = self._fit_metadata(
            context,
            max(
                1,
                int(
                    self._settings.max_context_characters
                    * self._settings.context_metadata_budget_ratio
                ),
            ),
        )
        metadata_json = json.dumps(metadata_payload, ensure_ascii=False, separators=(",", ":"))
        remainder = max(0, self._settings.max_context_characters - len(metadata_json))
        snapshot, recent, session_text = await self._ensure_coverage_before_shift(
            snapshot=snapshot,
            recent=recent,
            inbound=inbound,
            content=event.content,
            remainder=remainder,
            event_limit=runtime.context.local_event_limit,
            reset=reset,
            identity=history_identity,
            current_event=event,
        )
        history_window_key = self._history_window_key(
            history_identity, reset, coverage_end=snapshot.coverage_end, revision=snapshot.revision
        )
        history_budget = max(0, remainder - len(session_text))
        if snapshot.coverage_end > 0:
            history_budget = int(
                history_budget * self._settings.conversation_history_raw_tail_budget_ratio
            )
        bounded_messages = self._bounded_external_history(
            recent,
            current_event=event,
            bot_display_name=self._settings.bot_display_name,
            timezone=self._settings.default_timezone,
            character_budget=history_budget,
            event_limit=runtime.context.local_event_limit,
            low_watermark_ratio=self._settings.history_window_low_watermark_ratio,
            anchor_event_id=self._history_window_anchor(history_window_key),
            allow_shift=snapshot.coverage_end > 0,
        )
        self._remember_history_window_anchor(
            history_window_key,
            bounded_messages.history_anchor_event_id,
        )
        history = bounded_messages.history_messages
        current_message = bounded_messages.current_message
        current_time = await self._time.current(authorization_user_id)
        return AssembledContext(
            metadata_payload=metadata_payload,
            history_messages=history,
            current_message=current_message,
            recent_delivery=self._recent_delivery(recent, self._settings.default_timezone),
            current_time=current_time,
            current_relationship=None,
            metrics=ContextMetrics(
                metadata_characters=len(metadata_json),
                history_characters=sum(len(item.content or "") for item in history),
                history_messages=len(history),
                current_message_characters=len(current_message.content or ""),
                raw_history_window_shifted=bounded_messages.raw_history_window_shifted,
                rollup_characters=len(session_text),
                rollup_mode=snapshot.rollup_mode,
                covered_to=snapshot.coverage_end or None,
            ),
            visible_event_ids=bounded_messages.visible_event_ids,
            external_events=external_events,
            prompt_cache_key=history_window_key,
            history_anchor_event_id=bounded_messages.history_anchor_event_id,
            session_text=session_text,
            conversation_summary=snapshot.frontier or None,
        )

    @staticmethod
    def _recent_delivery(
        recent: tuple[EventRecord, ...],
        timezone: str = "Asia/Shanghai",
    ) -> tuple[dict[str, object], ...]:
        """Project confirmed outbound delivery metadata for the exact conversation."""

        delivered: list[dict[str, object]] = []
        for row in reversed(recent):
            if row.direction != "outbound" or not row.platform_message_id.strip():
                continue
            # Historical synthetic ids predate strict transport receipts and
            # cannot prove that a platform accepted the message.
            if row.platform_message_id.startswith(("out-", "agent-out-", "plugin-out-")):
                continue
            media_kinds: list[str] = []
            has_text = False
            for segment in row.segments:
                segment_type = str(segment.get("type", ""))
                data = segment.get("data")
                if segment_type == "text":
                    has_text = has_text or bool(
                        isinstance(data, dict) and str(data.get("text", "")).strip()
                    )
                elif segment_type == "record":
                    if "voice" not in media_kinds:
                        media_kinds.append("voice")
                elif segment_type == "image":
                    kind = (
                        "emoji_image"
                        if isinstance(data, dict) and bool(str(data.get("emoji_id", "")).strip())
                        else "image"
                    )
                    if kind not in media_kinds:
                        media_kinds.append(kind)
            delivered.append(
                {
                    "platform_message_id": row.platform_message_id,
                    "sent_at": local_iso(row.occurred_at, timezone),
                    "has_text": has_text,
                    "media_kinds": media_kinds,
                }
            )
            if len(delivered) >= 3:
                break
        delivered.reverse()
        return tuple(delivered)

    async def _available_memory_subjects(
        self,
        inbound: InboundMessage,
        current_profile: UserProfileSnapshot,
    ) -> list[dict[str, str]]:
        """Expose only backend-verifiable refs that memory tools can consume this turn."""

        subjects = [
            {
                "subject_ref": "current_speaker",
                "display_name": current_profile.display_name,
            }
        ]
        if self._settings.self_memory_enabled:
            subjects.append(
                {"subject_ref": "self", "display_name": self._settings.bot_display_name}
            )
        group_id = inbound.group_id
        if group_id is None:
            return subjects

        mentioned: list[str] = []
        for user_id in inbound.mentioned_user_ids:
            if not user_id or user_id in {inbound.sender.user_id, inbound.bot_user_id}:
                continue
            if user_id not in mentioned:
                mentioned.append(user_id)
            if len(mentioned) >= 5:
                break
        reply_user_id = inbound.reply_sender_user_id
        candidates = tuple(
            dict.fromkeys(
                (
                    *mentioned,
                    *(
                        (reply_user_id,)
                        if reply_user_id
                        and reply_user_id not in {inbound.sender.user_id, inbound.bot_user_id}
                        else ()
                    ),
                )
            )
        )
        members = await self._people.members_in_group(candidates, group_id)
        profiles = await self._people.get_many(tuple(members), group_id=group_id)

        for index, user_id in enumerate(mentioned, start=1):
            if user_id not in members:
                continue
            person = profiles.get(user_id)
            subjects.append(
                {
                    "subject_ref": f"mentioned_user_{index}",
                    "display_name": person.display_name if person else "被提及群成员",
                }
            )
        if reply_user_id in members:
            person = profiles.get(reply_user_id)
            subjects.append(
                {
                    "subject_ref": "replied_message_author",
                    "display_name": person.display_name if person else "被回复群成员",
                }
            )
        return subjects

    @staticmethod
    def relationship_json(snapshot: RelationshipSnapshot) -> dict[str, Any]:
        return {
            "affection_score": snapshot.affection_score,
            "trust_score": snapshot.trust_score,
            "effective_trust": snapshot.effective_trust,
            "relationship_weight": snapshot.relationship_weight,
            "stage": snapshot.stage.value,
        }

    def _memory_exposures(
        self,
        retrieval: Any,
        selected_fact_ids: tuple[int, ...],
    ) -> tuple[MemoryExposure, ...]:
        selected = set(selected_fact_ids)
        by_id: dict[int, MemoryExposure] = {}
        for block in retrieval.blocks:
            for hit in block.hits:
                fact = hit.fact
                if fact.id not in selected:
                    continue
                by_id[fact.id] = MemoryExposure(
                    memory_ref=f"M{fact.id}",
                    fact_id=fact.id,
                    kind=fact.kind.value,
                    category=fact.category[:64],
                    content=fact.content[:4_000],
                    occurred_at=(
                        local_iso(fact.valid_from, self._settings.default_timezone)
                        if fact.valid_from is not None
                        else None
                    ),
                    target_role=block.target.role.value,
                    source=MemoryExposureSource.AUTOMATIC,
                )
        return tuple(by_id[fact_id] for fact_id in selected_fact_ids if fact_id in by_id)

    @classmethod
    def _fit_metadata(
        cls,
        context: dict[str, Any],
        limit: int,
    ) -> tuple[dict[str, object], tuple[int, ...]]:
        """Select contributions and enforce the serialized metadata budget."""

        contributions = cls._context_contributions(context)
        selection_budget = limit
        while True:
            selection = ContextBudgeter().select(
                contributions,
                character_budget=selection_budget,
            )
            payload, selected_fact_ids = cls._render_metadata_selection(selection.selected)
            rendered_size = len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            )
            if rendered_size <= limit:
                return payload, selected_fact_ids
            # Contribution costs intentionally describe standalone items. Reduce the
            # selection budget by the exact container/aggregation overshoot and retry.
            selection_budget -= max(1, rendered_size - limit)

    @staticmethod
    def _render_metadata_selection(
        selection: tuple[ContextContribution, ...],
    ) -> tuple[dict[str, object], tuple[int, ...]]:
        selected = {
            item.id: ContextAssembler._public_context_payload(item.payload) for item in selection
        }
        items: list[dict[str, object]] = []
        selected_fact_ids: list[int] = []
        for item in selection:
            if isinstance(item.payload, dict):
                fact_id = item.payload.get("fact_id")
                if isinstance(fact_id, int) and fact_id > 0:
                    selected_fact_ids.append(fact_id)
            if item.id.startswith(
                (
                    "person_memory.",
                    "current_group.fact.",
                    "current_person_in_group.fact.",
                    "referenced_person_fact.",
                    "referenced_group_fact.",
                    "current_self.fact.",
                    "recent_external_event.",
                )
            ):
                continue
            payload = item.payload
            if item.id == "current_person" and isinstance(payload, dict):
                payload = {
                    **payload,
                    "facts": [
                        value for key, value in selected.items() if key.startswith("person_memory.")
                    ],
                }
            elif item.id in {"current_group", "current_person_in_group"} and isinstance(
                payload, dict
            ):
                payload = {
                    **payload,
                    "facts": [
                        value
                        for key, value in selected.items()
                        if key.startswith(f"{item.id}.fact.")
                    ],
                }
            items.append({"id": item.id, "data": payload})
        self_facts = [
            value for key, value in selected.items() if key.startswith("current_self.fact.")
        ]
        if self_facts:
            items.append({"id": "current_self", "data": {"facts": self_facts}})
        external_events = [
            value for key, value in selected.items() if key.startswith("recent_external_event.")
        ]
        if external_events:
            items.append(
                {
                    "id": "recent_external_events",
                    "data": {
                        "events": external_events,
                        "content_trust": "external_untrusted",
                    },
                }
            )
        for output_item in items:
            item_id = output_item["id"]
            payload = output_item["data"]
            if not isinstance(item_id, str) or not item_id.startswith("referenced_person."):
                continue
            if not isinstance(payload, dict):
                continue
            index = item_id.rsplit(".", 1)[-1]
            payload["person_facts"] = [
                value
                for key, value in selected.items()
                if key.startswith(f"referenced_person_fact.{index}.")
            ]
            payload["group_facts"] = [
                value
                for key, value in selected.items()
                if key.startswith(f"referenced_group_fact.{index}.")
            ]
        return {"items": items}, tuple(dict.fromkeys(selected_fact_ids))

    @staticmethod
    def _public_context_payload(payload: Any) -> Any:
        if isinstance(payload, dict):
            return {
                key: ContextAssembler._public_context_payload(value)
                for key, value in payload.items()
                if not str(key).startswith("_")
            }
        if isinstance(payload, list):
            return [ContextAssembler._public_context_payload(item) for item in payload]
        if isinstance(payload, tuple):
            return tuple(ContextAssembler._public_context_payload(item) for item in payload)
        return payload

    @staticmethod
    def _context_contributions(
        context: dict[str, Any],
    ) -> tuple[ContextContribution, ...]:
        items: list[ContextContribution] = []

        def add(
            item_id: str,
            payload: Any,
            *,
            priority: int,
            relevance: float,
            required: bool = False,
        ) -> None:
            cost = len(
                json.dumps(
                    {"id": item_id, "data": payload},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            )
            items.append(
                ContextContribution(
                    id=item_id,
                    priority=priority,
                    relevance=relevance,
                    cost=cost,
                    payload=payload,
                    required=required,
                )
            )

        def add_memory(item_id: str, payload: Any, *, fallback_priority: int) -> None:
            if not isinstance(payload, dict):
                add(item_id, payload, priority=fallback_priority, relevance=0.5)
                return
            public_payload = ContextAssembler._public_context_payload(payload)
            score = payload.get("_retrieval_score")
            pinned = payload.get("_retrieval_pinned") is True
            preference = payload.get("_preference_reserve") is True
            if isinstance(score, (int, float)) and (score > 0 or pinned or preference):
                add(
                    item_id,
                    public_payload,
                    priority=90 if pinned else 85 if preference else 80,
                    relevance=max(0.0, min(1.0, float(score))),
                )
                return
            importance = payload.get("importance", 1)
            add(
                item_id,
                public_payload,
                priority=fallback_priority + int(importance),
                relevance=0.8,
            )

        current = context.get("current_person")
        if isinstance(current, dict):
            base = {key: value for key, value in current.items() if key not in {"aliases", "facts"}}
            add("current_person", base, priority=100, relevance=1, required=True)
            for index, alias in enumerate(current.get("aliases", ())):
                add(f"current_alias.{index}", alias, priority=45, relevance=0.7)
            for index, memory in enumerate(current.get("facts", ())):
                add_memory(f"person_memory.{index}", memory, fallback_priority=60)
        add("scene", context.get("scene", {}), priority=100, relevance=1, required=True)
        current_self = context.get("current_self")
        if isinstance(current_self, dict):
            for index, memory in enumerate(current_self.get("facts", ())):
                add_memory(f"current_self.fact.{index}", memory, fallback_priority=70)
        memory_subjects = context.get("available_memory_subjects")
        if isinstance(memory_subjects, list) and memory_subjects:
            add(
                "available_memory_subjects",
                memory_subjects,
                priority=100,
                relevance=1,
            )
        for key, priority in (("current_group", 55), ("current_person_in_group", 65)):
            block = context.get(key)
            if not isinstance(block, dict):
                continue
            identity = {name: value for name, value in block.items() if name != "facts"}
            add(key, identity, priority=95, relevance=1, required=True)
            for index, value in enumerate(block.get("facts", ())):
                add_memory(f"{key}.fact.{index}", value, fallback_priority=priority)
        for index, person in enumerate(context.get("referenced_people", ())):
            if not isinstance(person, dict):
                continue
            identity = {
                key: value
                for key, value in person.items()
                if key not in {"person_facts", "group_facts"}
            }
            add(
                f"referenced_person.{index}",
                identity,
                priority=90,
                relevance=1,
                required=True,
            )
            for fact_index, fact in enumerate(person.get("person_facts", ())):
                add_memory(
                    f"referenced_person_fact.{index}.{fact_index}",
                    fact,
                    fallback_priority=58,
                )
            for fact_index, fact in enumerate(person.get("group_facts", ())):
                add_memory(
                    f"referenced_group_fact.{index}.{fact_index}",
                    fact,
                    fallback_priority=57,
                )
        for index, event in enumerate(context.get("recent_external_events", ())):
            add(
                f"recent_external_event.{index}",
                event,
                priority=75 + index,
                relevance=0.9,
            )
        return tuple(items)

    @staticmethod
    def _history_window_key(
        identity: ConversationIdentity,
        reset: datetime | None,
        *,
        coverage_end: int = 0,
        revision: int = 0,
    ) -> str:
        reset_marker = reset.isoformat() if reset is not None else "none"
        return f"{identity.key}|reset:{reset_marker}|cov:{coverage_end}|rev:{revision}"

    def _history_window_anchor(self, key: str) -> int | None:
        anchor = self._history_window_anchors.get(key)
        if anchor is not None:
            self._history_window_anchors.move_to_end(key)
        return anchor

    def _remember_history_window_anchor(self, key: str, anchor: int | None) -> None:
        if anchor is None:
            self._history_window_anchors.pop(key, None)
            return
        existing = self._history_window_anchors.get(key)
        # Concurrent turns may finish out of order. Event IDs are monotonic, so
        # never let an older turn move a conversation's cache anchor backwards.
        self._history_window_anchors[key] = max(existing or anchor, anchor)
        self._history_window_anchors.move_to_end(key)
        while len(self._history_window_anchors) > _HISTORY_WINDOW_STATE_LIMIT:
            self._history_window_anchors.popitem(last=False)

    def _rollup_enabled(self) -> bool:
        return (
            self._settings.conversation_history_rollup_enabled
            and self._history_repository is not None
        )

    @staticmethod
    def _conversation_history_identity(
        *,
        bot_user_id: str,
        scope_type: ScopeType,
        user_id: str,
        group_id: str | None,
        reset: datetime | None,
    ) -> ConversationHistoryIdentity:
        if scope_type is ScopeType.GROUP:
            return ConversationHistoryIdentity(
                bot_user_id=bot_user_id,
                scope_type=scope_type,
                group_id=group_id,
                reset_at=reset,
            )
        return ConversationHistoryIdentity(
            bot_user_id=bot_user_id,
            scope_type=scope_type,
            private_peer_user_id=user_id,
            reset_at=reset,
        )

    @staticmethod
    def _frontier_mode(frontier: tuple[ConversationHistorySummary, ...]) -> str | None:
        if not frontier:
            return None
        modes = tuple(dict.fromkeys(item.mode.value for item in frontier))
        return modes[0] if len(modes) == 1 else ",".join(modes)

    async def _load_history_snapshot(
        self,
        *,
        bot_user_id: str,
        scope_type: ScopeType,
        user_id: str,
        group_id: str | None,
        reset: datetime | None,
        event_limit: int,
    ) -> _HistoryPromptWindow:
        if not self._rollup_enabled() or self._history_repository is None:
            recent = await self._ledger.list_recent(
                scope_type=scope_type,
                user_id=user_id,
                group_id=group_id,
                limit=event_limit,
                since=reset,
            )
            return _HistoryPromptWindow(
                recent=recent,
                session_text="",
                coverage_end=0,
                revision=0,
                frontier=(),
                rollup_mode=None,
            )
        loaded = await self._history_repository.load_prompt_snapshot(
            self._conversation_history_identity(
                bot_user_id=bot_user_id,
                scope_type=scope_type,
                user_id=user_id,
                group_id=group_id,
                reset=reset,
            ),
            recent_limit=max(1, event_limit),
        )
        recent = tuple(item for item in loaded.recent_events if isinstance(item, EventRecord))
        return _HistoryPromptWindow(
            recent=recent,
            session_text=self._history_renderer.render_frontier(loaded.frontier),
            coverage_end=loaded.coverage_end_event_id,
            revision=loaded.revision,
            frontier=loaded.frontier,
            rollup_mode=self._frontier_mode(loaded.frontier),
        )

    async def _ensure_coverage_before_shift(
        self,
        *,
        snapshot: _HistoryPromptWindow,
        recent: tuple[EventRecord, ...],
        inbound: InboundMessage,
        content: str,
        remainder: int,
        event_limit: int,
        reset: datetime | None,
        identity: ConversationIdentity,
        current_event: EventRecord | None = None,
    ) -> tuple[_HistoryPromptWindow, tuple[EventRecord, ...], str]:
        session_text = snapshot.session_text
        coverage = self._history_coverage
        if not self._rollup_enabled() or snapshot.coverage_end > 0 or coverage is None:
            return snapshot, recent, session_text
        renderer = ChatEventPromptRenderer(
            recent,
            bot_display_name=self._settings.bot_display_name,
            timezone=self._settings.default_timezone,
        )
        if current_event is not None:
            history_rows = tuple(row for row in recent if row.id != current_event.id)
            current_chars = len(renderer.render_reference_event(current_event))
            record = current_event
            fallback = current_event.id
        else:
            history_rows = tuple(
                row for row in recent if row.platform_message_id != inbound.message_id
            )
            current_row = next(
                (row for row in reversed(recent) if row.platform_message_id == inbound.message_id),
                None,
            )
            if current_row is None:
                return snapshot, recent, session_text
            current_chars = len(
                renderer.reference_message(
                    current_row,
                    current_message_id=inbound.message_id,
                    current_content=content,
                ).content
                or ""
            )
            record = current_row
            fallback = current_row.id
        rendered = renderer.main_agent_history(history_rows)
        try:
            summary = await coverage.ensure_extractive_coverage(
                record,
                rendered=rendered,
                anchor_event_id=self._history_window_anchor(
                    self._history_window_key(
                        identity, reset, coverage_end=0, revision=snapshot.revision
                    )
                ),
                high_event_limit=max(0, event_limit - 1),
                high_character_limit=max(0, remainder - current_chars),
                fallback_anchor_event_id=fallback,
            )
        except (FrontierInvariantError, HistoryJobConflictError) as exc:
            logger.warning(
                "conversation_history_coverage_skipped error_category=%s",
                type(exc).__name__,
            )
            return snapshot, recent, session_text
        if summary is None:
            return snapshot, recent, session_text
        reloaded = await self._load_history_snapshot(
            bot_user_id=inbound.bot_user_id,
            scope_type=inbound.scope_type,
            user_id=(
                current_event.private_peer_user_id or inbound.sender.user_id
                if current_event is not None
                else inbound.sender.user_id
            ),
            group_id=inbound.group_id,
            reset=reset,
            event_limit=event_limit,
        )
        return reloaded, reloaded.recent, reloaded.session_text

    @classmethod
    def _bounded_history(
        cls,
        recent: tuple[EventRecord, ...],
        *,
        inbound: InboundMessage,
        content: str,
        character_budget: int,
        event_limit: int,
        low_watermark_ratio: float,
        anchor_event_id: int | None,
        bot_display_name: str = "Yuki",
        timezone: str = "Asia/Shanghai",
        allow_shift: bool = True,
    ) -> _BoundedMessages:
        renderer = ChatEventPromptRenderer(
            recent,
            bot_display_name=bot_display_name,
            timezone=timezone,
        )
        current_row = next(
            (row for row in reversed(recent) if row.platform_message_id == inbound.message_id),
            None,
        )
        current_message = (
            renderer.reference_message(
                current_row,
                current_message_id=inbound.message_id,
                current_content=content,
            )
            if current_row is not None
            else ChatMessage(
                role="user",
                content=renderer.render_reference_inbound(inbound, content),
            )
        )
        history_rows = tuple(row for row in recent if row.platform_message_id != inbound.message_id)
        rendered = renderer.main_agent_history(history_rows)
        selection = cls._select_history_window(
            rendered,
            anchor_event_id=anchor_event_id,
            high_event_limit=max(0, event_limit - 1),
            high_character_limit=max(0, character_budget - len(current_message.content or "")),
            low_watermark_ratio=low_watermark_ratio,
            fallback_anchor_event_id=current_row.id if current_row is not None else None,
            allow_shift=allow_shift,
        )
        return _BoundedMessages(
            history_messages=selection.messages,
            current_message=current_message,
            history_anchor_event_id=selection.anchor_event_id,
            raw_history_window_shifted=selection.shifted,
            visible_event_ids=frozenset(
                (*selection.event_ids, *((current_row.id,) if current_row is not None else ()))
            ),
        )

    @staticmethod
    def _select_history_window(
        rendered: tuple[tuple[int, tuple[int, ...], ChatMessage], ...],
        *,
        anchor_event_id: int | None,
        high_event_limit: int,
        high_character_limit: int,
        low_watermark_ratio: float,
        fallback_anchor_event_id: int | None,
        allow_shift: bool = True,
    ) -> _HistoryWindowSelection:
        """Keep one prefix stable until a high watermark forces a block roll.

        Commit 4 stops assemble from sliding via this helper; policy no longer
        reads it. Keep the function until the assembler rewrite lands.
        """

        anchor_index = next(
            (index for index, item in enumerate(rendered) if item[0] == anchor_event_id),
            None,
        )
        anchor_found = anchor_index is not None
        candidate = rendered[anchor_index:] if anchor_index is not None else rendered
        candidate_characters = sum(len(item.content or "") for _, _, item in candidate)
        must_roll = (
            not anchor_found
            or len(candidate) > high_event_limit
            or candidate_characters > high_character_limit
        )
        if not must_roll:
            return _HistoryWindowSelection(
                messages=tuple(item for _, _, item in candidate),
                anchor_event_id=(candidate[0][0] if candidate else fallback_anchor_event_id),
                shifted=False,
                event_ids=tuple(
                    event_id for _, event_ids, _ in candidate for event_id in event_ids
                ),
            )
        if not allow_shift:
            return _HistoryWindowSelection(
                messages=tuple(item for _, _, item in candidate),
                anchor_event_id=(candidate[0][0] if candidate else fallback_anchor_event_id),
                shifted=False,
                event_ids=tuple(
                    event_id for _, event_ids, _ in candidate for event_id in event_ids
                ),
            )

        if high_event_limit <= 0 or high_character_limit <= 0:
            return _HistoryWindowSelection(
                messages=(),
                anchor_event_id=fallback_anchor_event_id,
                shifted=anchor_event_id is not None,
            )

        low_event_limit = max(1, int(high_event_limit * low_watermark_ratio))
        low_character_limit = max(1, int(high_character_limit * low_watermark_ratio))
        selected_reversed: list[tuple[int, tuple[int, ...], ChatMessage]] = []
        selected_characters = 0
        for item in reversed(candidate):
            size = len(item[2].content or "")
            if len(selected_reversed) >= low_event_limit:
                break
            if not selected_reversed and size > high_character_limit:
                break
            if selected_reversed and selected_characters + size > low_character_limit:
                break
            selected_reversed.append(item)
            selected_characters += size
        selected = tuple(reversed(selected_reversed))
        return _HistoryWindowSelection(
            messages=tuple(item for _, _, item in selected),
            anchor_event_id=(selected[0][0] if selected else fallback_anchor_event_id),
            shifted=anchor_event_id is not None,
            event_ids=tuple(event_id for _, event_ids, _ in selected for event_id in event_ids),
        )

    def _external_event_context(
        self,
        recent: tuple[EventRecord, ...],
    ) -> tuple[dict[str, object], ...]:
        limit = self._settings.plugin_external_event_context_limit
        character_limit = self._settings.plugin_external_event_context_characters
        selected: list[dict[str, object]] = []
        used = 0
        for row in reversed(recent):
            if row.event_kind != "external_event":
                continue
            payload = row.external_payload or {}
            item: dict[str, object] = {
                "source": row.external_source or "external",
                "source_plugin_id": row.source_plugin_id or "",
                "event_type": row.external_event_type or "event",
                "summary": row.content[:4_000],
                "occurred_at": local_iso(row.occurred_at, self._settings.default_timezone),
                "payload": payload,
                "content_trust": "external_untrusted",
            }
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
            if len(encoded) > character_limit:
                item["payload"] = {}
                item["summary"] = row.content[: max(1, character_limit // 2)]
                encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if used + len(encoded) > character_limit:
                continue
            selected.append(item)
            used += len(encoded)
            if len(selected) >= limit:
                break
        selected.reverse()
        return tuple(selected)

    @classmethod
    def _bounded_external_history(
        cls,
        recent: tuple[EventRecord, ...],
        *,
        current_event: EventRecord,
        character_budget: int,
        event_limit: int,
        low_watermark_ratio: float,
        anchor_event_id: int | None,
        bot_display_name: str = "Yuki",
        timezone: str = "Asia/Shanghai",
        allow_shift: bool = True,
    ) -> _BoundedMessages:
        renderer = ChatEventPromptRenderer(
            recent,
            bot_display_name=bot_display_name,
            timezone=timezone,
        )
        trigger = renderer.render_reference_event(current_event)
        history_rows = tuple(row for row in recent if row.id != current_event.id)
        rendered = renderer.main_agent_history(history_rows)
        selection = cls._select_history_window(
            rendered,
            anchor_event_id=anchor_event_id,
            high_event_limit=max(0, event_limit - 1),
            high_character_limit=max(0, character_budget - len(trigger)),
            low_watermark_ratio=low_watermark_ratio,
            fallback_anchor_event_id=current_event.id,
            allow_shift=allow_shift,
        )
        return _BoundedMessages(
            history_messages=selection.messages,
            current_message=ChatMessage(role="system", content=trigger),
            history_anchor_event_id=selection.anchor_event_id,
            raw_history_window_shifted=selection.shifted,
            visible_event_ids=frozenset((*selection.event_ids, current_event.id)),
        )
