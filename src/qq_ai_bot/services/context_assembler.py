"""Bounded person-centric context assembly for one normal chat Agent."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
from qq_ai_bot.conversation.rollup.models import ConversationRollupState
from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
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
    history_anchor_event_id: int | None = None
    rollup_text: str = ""
    prompt_scope_id: int = 0
    prompt_scope_key: str = ""
    prompt_generation: int = 0
    prompt_effective_coverage: int = 0
    prompt_rollup_revision: int = 0
    prompt_raw_tail_end_event_id: int = 0


@dataclass(frozen=True, slots=True)
class _BoundedMessages:
    """A bounded history window plus the separately preserved current input."""

    history_messages: tuple[ChatMessage, ...]
    current_message: ChatMessage
    history_anchor_event_id: int | None
    raw_history_window_shifted: bool
    visible_event_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class _HistoryPromptWindow:
    """One consistent rollup checkpoint plus its continuous uncovered suffix."""

    recent: tuple[EventRecord, ...]
    rollup_text: str
    coverage_end: int
    revision: int
    rollup: ConversationRollupState | None
    rollup_mode: str | None


@dataclass(frozen=True, slots=True)
class _UncoveredPromptView:
    """Rendered uncovered history used to decide sync extractive."""

    history_rows: tuple[EventRecord, ...]
    rendered: tuple[tuple[int, tuple[int, ...], ChatMessage], ...]
    record: EventRecord
    fallback_event_id: int
    current_characters: int
    rendered_characters: int


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
        rollup_repository: ConversationRollupRepository,
        rollup_service: ConversationRollupService,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._people = people
        self._memory_context = memory_context
        self._relationships = relationships
        self._time = time_service
        self._rollups = rollup_repository
        self._rollup_service = rollup_service

    async def assemble(
        self,
        *,
        inbound: InboundMessage,
        identity: ConversationScope,
        profile: UserProfileSnapshot,
        turn: ConversationTurnSnapshot,
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

        await self._ensure_lightweight_backlog(
            identity,
            turn,
            event_limit=runtime.context.local_event_limit,
        )
        current_event = await self._ledger.get_event(turn.trigger_event_id)
        if (
            current_event is None
            or current_event.bot_user_id != identity.bot_user_id
            or current_event.platform_message_id != inbound.message_id
        ):
            raise ConversationCoverageError("turn trigger event does not match scope snapshot")
        snapshot = await self._load_history_snapshot(
            identity,
            turn=turn,
            before_event_id=current_event.id,
        )
        recent = snapshot.recent
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
        snapshot, recent, rollup_text, shifted = await self._ensure_uncovered_fits_budget(
            snapshot=snapshot,
            recent=recent,
            inbound=inbound,
            content=content,
            remainder=remainder,
            event_limit=runtime.context.local_event_limit,
            identity=identity,
            current_event=current_event,
            turn=turn,
        )
        bounded_messages = self._bounded_history(
            recent,
            inbound=inbound,
            content=content,
            current_event=current_event,
            bot_display_name=self._settings.bot_display_name,
            timezone=self._settings.default_timezone,
            raw_history_window_shifted=shifted,
        )
        history_messages = bounded_messages.history_messages
        current_message = bounded_messages.current_message
        history_characters = sum(len(message.content or "") for message in history_messages)
        uncovered_events = len(
            tuple(row for row in recent if row.platform_message_id != inbound.message_id)
        )
        over_budget = int(
            history_characters
            > self._near_window_character_budget(
                remainder=remainder,
                rollup_text=rollup_text,
                coverage_end=snapshot.coverage_end,
            )
            or uncovered_events > max(0, runtime.context.local_event_limit - 1)
        )
        metrics = ContextMetrics(
            metadata_characters=len(metadata_json),
            history_characters=history_characters,
            history_messages=len(history_messages),
            current_message_characters=len(current_message.content or ""),
            raw_history_window_shifted=bounded_messages.raw_history_window_shifted,
            rollup_characters=len(rollup_text),
            rollup_mode=snapshot.rollup_mode,
            covered_to=snapshot.coverage_end or None,
        )
        logger.debug(
            "context_assembled metadata_characters=%d history_characters=%d "
            "history_messages=%d current_message_characters=%d "
            "raw_history_window_shifted=%s rollup_characters=%d "
            "uncovered_events=%d over_budget=%d",
            metrics.metadata_characters,
            metrics.history_characters,
            metrics.history_messages,
            metrics.current_message_characters,
            metrics.raw_history_window_shifted,
            metrics.rollup_characters,
            uncovered_events,
            over_budget,
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
            history_anchor_event_id=bounded_messages.history_anchor_event_id,
            rollup_text=rollup_text,
            prompt_scope_id=turn.scope_id,
            prompt_scope_key=turn.scope_key,
            prompt_generation=turn.generation,
            prompt_effective_coverage=snapshot.coverage_end,
            prompt_rollup_revision=snapshot.revision,
            prompt_raw_tail_end_event_id=(recent[-1].id if recent else snapshot.coverage_end),
        )

    async def assemble_external(
        self,
        *,
        event: EventRecord,
        turn: ConversationTurnSnapshot,
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
            ConversationScope.group(event.bot_user_id, event.group_id)
            if event.scope_type is ScopeType.GROUP and event.group_id is not None
            else ConversationScope.private(
                event.bot_user_id, event.private_peer_user_id or authorization_user_id
            )
        )
        await self._ensure_lightweight_backlog(
            history_identity,
            turn,
            event_limit=runtime.context.local_event_limit,
        )
        snapshot = await self._load_history_snapshot(
            history_identity,
            turn=turn,
            before_event_id=event.id,
        )
        recent = snapshot.recent
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
        snapshot, recent, rollup_text, shifted = await self._ensure_uncovered_fits_budget(
            snapshot=snapshot,
            recent=recent,
            inbound=inbound,
            content=event.content,
            remainder=remainder,
            event_limit=runtime.context.local_event_limit,
            identity=history_identity,
            current_event=event,
            turn=turn,
        )
        bounded_messages = self._bounded_history(
            recent,
            inbound=inbound,
            content=event.content,
            current_event=event,
            bot_display_name=self._settings.bot_display_name,
            timezone=self._settings.default_timezone,
            raw_history_window_shifted=shifted,
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
                rollup_characters=len(rollup_text),
                rollup_mode=snapshot.rollup_mode,
                covered_to=snapshot.coverage_end or None,
            ),
            visible_event_ids=bounded_messages.visible_event_ids,
            external_events=external_events,
            history_anchor_event_id=bounded_messages.history_anchor_event_id,
            rollup_text=rollup_text,
            prompt_scope_id=turn.scope_id,
            prompt_scope_key=turn.scope_key,
            prompt_generation=turn.generation,
            prompt_effective_coverage=snapshot.coverage_end,
            prompt_rollup_revision=snapshot.revision,
            prompt_raw_tail_end_event_id=(recent[-1].id if recent else snapshot.coverage_end),
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

    def _near_window_character_budget(
        self,
        *,
        remainder: int,
        rollup_text: str,
        coverage_end: int,
    ) -> int:
        return self._prompt_character_admit(
            remainder=remainder,
            rollup_text=rollup_text,
            coverage_end=coverage_end,
        )

    def _near_window_event_limit(self, *, event_limit: int, coverage_end: int) -> int:
        return self._prompt_event_admit(event_limit=event_limit, coverage_end=coverage_end)

    def _prompt_event_admit(self, *, event_limit: int, coverage_end: int) -> int:
        ceiling = max(0, event_limit - 1)
        if coverage_end <= 0:
            return ceiling
        return min(
            ceiling,
            self._settings.conversation_rollup_raw_tail_events
            + self._settings.conversation_rollup_trigger_events,
        )

    def _prompt_event_target(self, *, event_limit: int, coverage_end: int) -> int:
        admit = self._prompt_event_admit(event_limit=event_limit, coverage_end=coverage_end)
        if coverage_end <= 0:
            return admit
        return min(
            admit,
            self._settings.conversation_rollup_raw_tail_events
            + self._settings.conversation_rollup_stop_events,
        )

    def _prompt_character_admit(
        self,
        *,
        remainder: int,
        rollup_text: str,
        coverage_end: int,
    ) -> int:
        history_balance = max(0, remainder - len(rollup_text))
        if coverage_end <= 0:
            return history_balance
        return min(
            history_balance,
            self._settings.conversation_rollup_raw_tail_characters
            + self._settings.conversation_rollup_trigger_characters,
        )

    def _prompt_character_target(
        self,
        *,
        remainder: int,
        rollup_text: str,
        coverage_end: int,
    ) -> int:
        admit = self._prompt_character_admit(
            remainder=remainder,
            rollup_text=rollup_text,
            coverage_end=coverage_end,
        )
        if coverage_end <= 0:
            return admit
        return min(
            admit,
            self._settings.conversation_rollup_raw_tail_characters
            + self._settings.conversation_rollup_stop_characters,
        )

    @staticmethod
    def _uncovered_fits_window(
        view: _UncoveredPromptView,
        *,
        event_limit: int,
        character_budget: int,
    ) -> bool:
        return len(view.history_rows) <= event_limit and view.rendered_characters <= max(
            0, character_budget - view.current_characters
        )

    async def _load_history_snapshot(
        self,
        scope: ConversationScope,
        *,
        turn: ConversationTurnSnapshot,
        before_event_id: int | None,
    ) -> _HistoryPromptWindow:
        loaded = await self._rollups.load_prompt_snapshot(
            scope,
            before_event_id=before_event_id,
        )
        rollup = loaded.rollup
        if (
            loaded.scope.id != turn.scope_id
            or loaded.scope.scope.key != turn.scope_key
            or loaded.scope.generation != turn.generation
        ):
            raise ConversationCoverageError("prompt snapshot generation changed")
        return _HistoryPromptWindow(
            recent=loaded.raw_events,
            rollup_text=rollup.summary_text if rollup is not None else "",
            coverage_end=loaded.effective_coverage,
            revision=rollup.revision if rollup is not None else 0,
            rollup=rollup,
            rollup_mode=rollup.summary_kind.value if rollup is not None else None,
        )

    def _uncovered_prompt_view(
        self,
        recent: tuple[EventRecord, ...],
        *,
        inbound: InboundMessage,
        content: str,
        current_event: EventRecord | None,
    ) -> _UncoveredPromptView | None:
        renderer = ChatEventPromptRenderer(
            recent,
            bot_display_name=self._settings.bot_display_name,
            timezone=self._settings.default_timezone,
        )
        if current_event is not None:
            history_rows = tuple(row for row in recent if row.id != current_event.id)
            current_characters = len(renderer.render_reference_event(current_event))
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
                return None
            current_characters = len(
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
        return _UncoveredPromptView(
            history_rows=history_rows,
            rendered=rendered,
            record=record,
            fallback_event_id=fallback,
            current_characters=current_characters,
            rendered_characters=sum(len(item.content or "") for _, _, item in rendered),
        )

    async def _ensure_uncovered_fits_budget(
        self,
        *,
        snapshot: _HistoryPromptWindow,
        recent: tuple[EventRecord, ...],
        inbound: InboundMessage,
        content: str,
        remainder: int,
        event_limit: int,
        identity: ConversationScope,
        current_event: EventRecord | None = None,
        turn: ConversationTurnSnapshot,
    ) -> tuple[_HistoryPromptWindow, tuple[EventRecord, ...], str, bool]:
        coverage_before = snapshot.coverage_end
        rollup_text = snapshot.rollup_text
        if not self._settings.conversation_rollup_enabled:
            return snapshot, recent, rollup_text, False
        compact_to_stop = False
        max_batches = self._settings.conversation_rollup_foreground_max_batches
        for _ in range(max_batches):
            view = self._uncovered_prompt_view(
                recent,
                inbound=inbound,
                content=content,
                current_event=current_event,
            )
            if view is None:
                break
            event_cap = (
                self._prompt_event_target(
                    event_limit=event_limit,
                    coverage_end=snapshot.coverage_end,
                )
                if compact_to_stop
                else self._prompt_event_admit(
                    event_limit=event_limit,
                    coverage_end=snapshot.coverage_end,
                )
            )
            character_cap = (
                self._prompt_character_target(
                    remainder=remainder,
                    rollup_text=rollup_text,
                    coverage_end=snapshot.coverage_end,
                )
                if compact_to_stop
                else self._prompt_character_admit(
                    remainder=remainder,
                    rollup_text=rollup_text,
                    coverage_end=snapshot.coverage_end,
                )
            )
            if self._uncovered_fits_window(
                view, event_limit=event_cap, character_budget=character_cap
            ):
                break
            compact_to_stop = True
            committed = await self._rollup_service.ensure_extractive_coverage(
                repository=self._rollups,
                scope=identity,
                lease_seconds=self._settings.conversation_rollup_lease_seconds,
                max_batches=1,
            )
            if not committed:
                raise ConversationCoverageError(
                    "raw history is over budget but no continuous prefix is compressible"
                )
            snapshot = await self._load_history_snapshot(
                identity,
                turn=turn,
                before_event_id=current_event.id if current_event is not None else None,
            )
            recent = snapshot.recent
            rollup_text = snapshot.rollup_text
        final_view = self._uncovered_prompt_view(
            recent,
            inbound=inbound,
            content=content,
            current_event=current_event,
        )
        if final_view is not None:
            final_event_admit = self._prompt_event_admit(
                event_limit=event_limit,
                coverage_end=snapshot.coverage_end,
            )
            final_character_admit = self._prompt_character_admit(
                remainder=remainder,
                rollup_text=rollup_text,
                coverage_end=snapshot.coverage_end,
            )
            if not self._uncovered_fits_window(
                final_view,
                event_limit=final_event_admit,
                character_budget=final_character_admit,
            ):
                raise ConversationCoverageError(
                    "foreground coverage limit exhausted before prompt became bounded"
                )
        return snapshot, recent, rollup_text, snapshot.coverage_end > coverage_before

    async def _ensure_lightweight_backlog(
        self,
        scope: ConversationScope,
        turn: ConversationTurnSnapshot,
        *,
        event_limit: int,
    ) -> None:
        """Bound raw backlog from counters before loading any event bodies."""

        if not self._settings.conversation_rollup_enabled:
            return
        event_admit = self._prompt_event_admit(event_limit=event_limit, coverage_end=1)
        event_target = self._prompt_event_target(event_limit=event_limit, coverage_end=1)
        character_admit = (
            self._settings.conversation_rollup_raw_tail_characters
            + self._settings.conversation_rollup_trigger_characters
        )
        character_target = (
            self._settings.conversation_rollup_raw_tail_characters
            + self._settings.conversation_rollup_stop_characters
        )
        compact_to_stop = False
        for _ in range(self._settings.conversation_rollup_foreground_max_batches + 1):
            state, _rollup, _job = await self._rollups.status(scope)
            if state is None:
                raise ConversationCoverageError("conversation scope does not exist")
            if (
                state.id != turn.scope_id
                or state.generation != turn.generation
                or state.scope.key != turn.scope_key
            ):
                raise ConversationCoverageError("turn generation changed before prompt snapshot")
            event_cap = event_target if compact_to_stop else event_admit
            character_cap = character_target if compact_to_stop else character_admit
            if (
                state.uncovered_event_count <= event_cap
                and state.uncovered_character_count <= character_cap
            ):
                return
            compact_to_stop = True
            committed = await self._rollup_service.ensure_extractive_coverage(
                repository=self._rollups,
                scope=scope,
                lease_seconds=self._settings.conversation_rollup_lease_seconds,
                max_batches=1,
            )
            if not committed:
                raise ConversationCoverageError(
                    "raw backlog is unbounded but no continuous prefix is compressible"
                )
        raise ConversationCoverageError(
            "foreground coverage limit exhausted before loading prompt history"
        )

    @staticmethod
    def _bounded_history(
        recent: tuple[EventRecord, ...],
        *,
        inbound: InboundMessage,
        content: str,
        current_event: EventRecord | None = None,
        bot_display_name: str = "Yuki",
        timezone: str = "Asia/Shanghai",
        raw_history_window_shifted: bool = False,
    ) -> _BoundedMessages:
        renderer = ChatEventPromptRenderer(
            (*recent, *((current_event,) if current_event is not None else ())),
            bot_display_name=bot_display_name,
            timezone=timezone,
        )
        current_row = current_event or next(
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
        history_rows = tuple(
            row
            for row in recent
            if row.platform_message_id != inbound.message_id
            and (current_event is None or row.id != current_event.id)
        )
        rendered = renderer.main_agent_history(history_rows)
        event_ids = tuple(event_id for _, ids, _ in rendered for event_id in ids)
        return _BoundedMessages(
            history_messages=tuple(item for _, _, item in rendered),
            current_message=current_message,
            history_anchor_event_id=(
                rendered[0][0]
                if rendered
                else (current_row.id if current_row is not None else None)
            ),
            raw_history_window_shifted=raw_history_window_shifted,
            visible_event_ids=frozenset(
                (*event_ids, *((current_row.id,) if current_row is not None else ()))
            ),
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
