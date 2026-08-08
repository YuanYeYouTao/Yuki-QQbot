"""Bounded person-centric context assembly for one normal chat Agent."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    InboundMessage,
    SenderIdentity,
)
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.domain.relationships import RelationshipSnapshot
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.memory.context import (
    MemoryContextService,
    retrieval_fact_context,
    self_retrieval_fact_context,
)
from qq_ai_bot.memory.enums import MemoryContextMode, MemoryTargetRole
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    EventRecord,
    PeopleRepository,
    RelationshipRepository,
)
from qq_ai_bot.prompting import ContextBudgeter, ContextContribution
from qq_ai_bot.references import (
    MainAgentHistoryProjector,
    ReferenceEpochManager,
    TurnReferenceRegistry,
)
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
    history_window_rolled: bool
    history_event_count: int = 0
    history_block_count: int = 0
    history_envelope_characters: int = 0
    history_body_characters: int = 0
    reference_registry_user_count: int = 0
    reference_registry_message_count: int = 0
    reference_registry_group_count: int = 0
    reference_epoch_rolled: bool = False
    reference_epoch_rolls: int = 0


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
    external_events: tuple[dict[str, object], ...] = ()
    references: TurnReferenceRegistry | None = None


@dataclass(frozen=True, slots=True)
class _BoundedMessages:
    """A bounded history window plus the separately preserved current input."""

    history_messages: tuple[ChatMessage, ...]
    current_message: ChatMessage
    history_anchor_event_id: int | None
    history_window_rolled: bool
    references: TurnReferenceRegistry | None = None
    history_event_count: int = 0
    history_envelope_characters: int = 0
    history_body_characters: int = 0


@dataclass(frozen=True, slots=True)
class _HistoryWindowSelection:
    """One stable history epoch selected between configurable watermarks."""

    messages: tuple[ChatMessage, ...]
    anchor_event_id: int | None
    rolled: bool


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
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._people = people
        self._memory_context = memory_context
        self._relationships = relationships
        self._time = time_service
        self._history_window_anchors: OrderedDict[str, int] = OrderedDict()
        self._reference_epochs = ReferenceEpochManager(maximum_states=_HISTORY_WINDOW_STATE_LIMIT)

    async def assemble(
        self,
        *,
        inbound: InboundMessage,
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        content: str,
        runtime: RuntimeConfigSnapshot,
        planner_intent: str = "",
        memory_mode: MemoryContextMode = MemoryContextMode.LEXICAL,
        self_recall: bool = False,
    ) -> AssembledContext:
        """Build one bounded snapshot without persisting model-only metadata."""

        reset = await self._ledger.context_reset(identity)
        history_window_key = self._history_window_key(identity, reset)
        recent = await self._ledger.list_recent(
            scope_type=inbound.scope_type,
            user_id=inbound.sender.user_id,
            group_id=inbound.group_id,
            limit=runtime.context.local_event_limit,
            since=reset,
        )
        external_events = self._external_event_context(recent)
        retrieval = await self._memory_context.retrieve_for_turn(
            inbound=inbound,
            content=content,
            planner_intent=planner_intent,
            runtime=runtime,
            memory_mode=memory_mode,
            self_recall=self_recall,
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
                    retrieval_fact_context(hit)
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
                "facts": [self_retrieval_fact_context(hit) for hit in self_hits]
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
                    retrieval_fact_context(hit)
                    for hit in hits_by_role.get(MemoryTargetRole.CURRENT_PERSON_GROUP, ())
                ],
            }
            context["current_group"] = {
                "group_id": inbound.group_id,
                "facts": [
                    retrieval_fact_context(hit)
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
                entry[key] = [retrieval_fact_context(hit) for hit in block.hits]
            if referenced:
                context["referenced_people"] = list(referenced.values())

        total_budget = self._settings.max_context_characters
        reference_registry: TurnReferenceRegistry | None = None
        raw_context = context
        if self._settings.main_agent_reference_envelope_enabled:
            current_anchor = self._history_window_anchor(history_window_key)
            current_row = next(
                (row for row in reversed(recent) if row.platform_message_id == inbound.message_id),
                None,
            )
            reference_registry = self._reference_epochs.prepare(
                conversation_key=history_window_key,
                events=tuple(
                    row
                    for row in recent
                    if row.platform_message_id != inbound.message_id
                    and (current_anchor is None or row.id >= current_anchor)
                ),
                inbound=inbound,
                current_event_id=current_row.id if current_row is not None else -1,
                anchor_event_id=current_anchor,
                reset_marker=reset.isoformat() if reset is not None else "none",
            )
            context = reference_registry.project_value(context)
        metadata_budget = max(
            1,
            int(total_budget * self._settings.context_metadata_budget_ratio),
        )
        metadata_payload, selected_fact_ids = self._fit_metadata(context, metadata_budget)
        metadata_json = json.dumps(
            metadata_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        history_budget = max(0, total_budget - len(metadata_json))
        if reference_registry is not None:
            bounded_messages = self._bounded_main_history(
                recent,
                inbound=inbound,
                content=content,
                character_budget=history_budget,
                event_limit=runtime.context.local_event_limit,
                low_watermark_ratio=self._settings.history_window_low_watermark_ratio,
                anchor_event_id=self._history_window_anchor(history_window_key),
                conversation_key=history_window_key,
                reset_marker=reset.isoformat() if reset is not None else "none",
                registry=reference_registry,
            )
            if (
                bounded_messages.references is not None
                and bounded_messages.references.epoch_id != reference_registry.epoch_id
            ):
                reference_registry = bounded_messages.references
                context = reference_registry.project_value(raw_context)
                metadata_payload, selected_fact_ids = self._fit_metadata(context, metadata_budget)
                metadata_json = json.dumps(
                    metadata_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
        else:
            bounded_messages = self._bounded_history(
                recent,
                inbound=inbound,
                content=content,
                character_budget=history_budget,
                event_limit=runtime.context.local_event_limit,
                low_watermark_ratio=self._settings.history_window_low_watermark_ratio,
                anchor_event_id=self._history_window_anchor(history_window_key),
            )
        await self._memory_context.mark_used(retrieval, selected_fact_ids)
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
            history_window_rolled=bounded_messages.history_window_rolled,
            history_event_count=bounded_messages.history_event_count,
            history_block_count=len(history_messages),
            history_envelope_characters=bounded_messages.history_envelope_characters,
            history_body_characters=bounded_messages.history_body_characters,
            reference_registry_user_count=(
                len(reference_registry.users) if reference_registry is not None else 0
            ),
            reference_registry_message_count=(
                len(reference_registry.messages) if reference_registry is not None else 0
            ),
            reference_registry_group_count=(
                len(reference_registry.groups) if reference_registry is not None else 0
            ),
            reference_epoch_rolled=(
                reference_registry.epoch_rolled if reference_registry is not None else False
            ),
            reference_epoch_rolls=int(
                reference_registry.epoch_rolled if reference_registry is not None else False
            ),
        )
        logger.debug(
            "context_assembled metadata_characters=%d history_characters=%d "
            "history_messages=%d history_events=%d history_blocks=%d "
            "history_envelope_characters=%d history_body_characters=%d "
            "reference_users=%d reference_messages=%d reference_groups=%d "
            "current_message_characters=%d history_window_rolled=%s "
            "reference_epoch_rolled=%s reference_epoch_rolls=%d",
            metrics.metadata_characters,
            metrics.history_characters,
            metrics.history_messages,
            metrics.history_event_count,
            metrics.history_block_count,
            metrics.history_envelope_characters,
            metrics.history_body_characters,
            metrics.reference_registry_user_count,
            metrics.reference_registry_message_count,
            metrics.reference_registry_group_count,
            metrics.current_message_characters,
            metrics.history_window_rolled,
            metrics.reference_epoch_rolled,
            metrics.reference_epoch_rolls,
        )
        return AssembledContext(
            metadata_payload=metadata_payload,
            history_messages=history_messages,
            current_message=current_message,
            recent_delivery=self._recent_delivery(recent),
            current_time=current_time,
            current_relationship=current_relationship,
            metrics=metrics,
            external_events=external_events,
            references=reference_registry,
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
        recent = await self._ledger.list_recent(
            scope_type=event.scope_type,
            user_id=event.private_peer_user_id or authorization_user_id,
            group_id=event.group_id,
            limit=runtime.context.local_event_limit,
        )
        history_identity = (
            ConversationIdentity.group(event.group_id, authorization_user_id)
            if event.scope_type is ScopeType.GROUP and event.group_id is not None
            else ConversationIdentity.private(event.private_peer_user_id or authorization_user_id)
        )
        history_window_key = self._history_window_key(history_identity, None)
        retrieval = await self._memory_context.retrieve_for_turn(
            inbound=inbound,
            content=event.content,
            planner_intent=agent_intent,
            runtime=runtime,
            memory_mode=MemoryContextMode.LEXICAL,
            self_recall=True,
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
                "facts": [retrieval_fact_context(hit) for hit in group_hits],
            }
        self_hits = hits_by_role.get(MemoryTargetRole.CURRENT_SELF, ())
        if self_hits:
            context["current_self"] = {
                "facts": [self_retrieval_fact_context(hit) for hit in self_hits]
            }
        external_events = self._external_event_context(recent)
        if external_events:
            context["recent_external_events"] = list(external_events)
        metadata_payload, selected_fact_ids = self._fit_metadata(
            context,
            max(
                1,
                int(
                    self._settings.max_context_characters
                    * self._settings.context_metadata_budget_ratio
                ),
            ),
        )
        await self._memory_context.mark_used(retrieval, selected_fact_ids)
        metadata_characters = len(
            json.dumps(metadata_payload, ensure_ascii=False, separators=(",", ":"))
        )
        bounded_messages = self._bounded_external_history(
            recent,
            current_event=event,
            character_budget=max(
                0,
                self._settings.max_context_characters - metadata_characters,
            ),
            event_limit=runtime.context.local_event_limit,
            low_watermark_ratio=self._settings.history_window_low_watermark_ratio,
            anchor_event_id=self._history_window_anchor(history_window_key),
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
            recent_delivery=self._recent_delivery(recent),
            current_time=current_time,
            current_relationship=None,
            metrics=ContextMetrics(
                metadata_characters=metadata_characters,
                history_characters=sum(len(item.content or "") for item in history),
                history_messages=len(history),
                current_message_characters=len(current_message.content or ""),
                history_window_rolled=bounded_messages.history_window_rolled,
            ),
            external_events=external_events,
        )

    @staticmethod
    def _recent_delivery(
        recent: tuple[EventRecord, ...],
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
                    "sent_at": row.occurred_at.isoformat(),
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
            subjects.append({"subject_ref": "self", "display_name": "Yuki"})
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
        selected = {item.id: item.payload for item in selection}
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

        current = context.get("current_person")
        if isinstance(current, dict):
            base = {key: value for key, value in current.items() if key not in {"aliases", "facts"}}
            add("current_person", base, priority=100, relevance=1, required=True)
            for index, alias in enumerate(current.get("aliases", ())):
                add(f"current_alias.{index}", alias, priority=45, relevance=0.7)
            for index, memory in enumerate(current.get("facts", ())):
                importance = memory.get("importance", 1) if isinstance(memory, dict) else 1
                add(
                    f"person_memory.{index}",
                    memory,
                    priority=60 + int(importance),
                    relevance=0.9,
                )
        add("scene", context.get("scene", {}), priority=100, relevance=1, required=True)
        current_self = context.get("current_self")
        if isinstance(current_self, dict):
            for index, memory in enumerate(current_self.get("facts", ())):
                importance = memory.get("importance", 1) if isinstance(memory, dict) else 1
                add(
                    f"current_self.fact.{index}",
                    memory,
                    priority=70 + int(importance),
                    relevance=0.95,
                )
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
                add(f"{key}.fact.{index}", value, priority=priority, relevance=0.8)
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
                add(
                    f"referenced_person_fact.{index}.{fact_index}",
                    fact,
                    priority=58,
                    relevance=0.85,
                )
            for fact_index, fact in enumerate(person.get("group_facts", ())):
                add(
                    f"referenced_group_fact.{index}.{fact_index}",
                    fact,
                    priority=57,
                    relevance=0.85,
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
    ) -> str:
        reset_marker = reset.isoformat() if reset is not None else "none"
        return f"{identity.key}|reset:{reset_marker}"

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

    def _bounded_main_history(
        self,
        recent: tuple[EventRecord, ...],
        *,
        inbound: InboundMessage,
        content: str,
        character_budget: int,
        event_limit: int,
        low_watermark_ratio: float,
        anchor_event_id: int | None,
        conversation_key: str,
        reset_marker: str,
        registry: TurnReferenceRegistry,
    ) -> _BoundedMessages:
        """Compress trusted events before applying the rolling history budget."""

        current_row = next(
            (row for row in reversed(recent) if row.platform_message_id == inbound.message_id),
            None,
        )
        history_rows = tuple(
            row
            for row in recent
            if row.platform_message_id != inbound.message_id
            and (anchor_event_id is None or row.id >= anchor_event_id)
        )
        if event_limit > 1 and len(history_rows) > event_limit - 1:
            low_event_limit = max(
                1,
                int((event_limit - 1) * low_watermark_ratio),
            )
            history_rows = history_rows[-low_event_limit:]
        projector = MainAgentHistoryProjector(recent)
        current_message = projector.current_message(
            inbound=inbound,
            content=content,
            registry=registry,
            current_row=current_row,
        )
        blocks = projector.project(history_rows, registry)
        selection = self._select_history_window(
            tuple((block.first_event_id, block.message) for block in blocks),
            anchor_event_id=anchor_event_id,
            high_event_limit=max(0, event_limit - 1),
            high_character_limit=max(0, character_budget - len(current_message.content or "")),
            low_watermark_ratio=low_watermark_ratio,
            fallback_anchor_event_id=current_row.id if current_row is not None else None,
            event_weights={block.first_event_id: len(block.event_ids) for block in blocks},
        )
        selected_blocks = tuple(
            block
            for block in blocks
            if selection.anchor_event_id is not None
            and block.first_event_id >= selection.anchor_event_id
        )
        trimmed_initial_epoch = bool(
            blocks
            and selection.anchor_event_id is not None
            and selection.anchor_event_id != blocks[0].first_event_id
        )
        final_registry = registry
        if selection.rolled or trimmed_initial_epoch:
            selected_event_ids = {
                event_id for block in selected_blocks for event_id in block.event_ids
            }
            selected_rows = tuple(row for row in history_rows if row.id in selected_event_ids)
            final_registry = self._reference_epochs.prepare(
                conversation_key=conversation_key,
                events=selected_rows,
                inbound=inbound,
                current_event_id=current_row.id if current_row is not None else -1,
                anchor_event_id=selection.anchor_event_id,
                reset_marker=reset_marker,
                force_roll=True,
            )
            projector = MainAgentHistoryProjector(
                (*selected_rows, *((current_row,) if current_row else ()))
            )
            selected_blocks = projector.project(selected_rows, final_registry)
            selection = _HistoryWindowSelection(
                messages=tuple(block.message for block in selected_blocks),
                anchor_event_id=selection.anchor_event_id,
                rolled=True,
            )
            current_message = projector.current_message(
                inbound=inbound,
                content=content,
                registry=final_registry,
                current_row=current_row,
            )
        else:
            selected_messages = set(selection.messages)
            selected_blocks = tuple(block for block in blocks if block.message in selected_messages)
        return _BoundedMessages(
            history_messages=selection.messages,
            current_message=current_message,
            history_anchor_event_id=selection.anchor_event_id,
            history_window_rolled=selection.rolled,
            references=final_registry,
            history_event_count=sum(len(block.event_ids) for block in selected_blocks),
            history_envelope_characters=sum(block.envelope_characters for block in selected_blocks),
            history_body_characters=sum(block.body_characters for block in selected_blocks),
        )

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
    ) -> _BoundedMessages:
        renderer = ChatEventPromptRenderer(recent)
        current_row = next(
            (row for row in reversed(recent) if row.platform_message_id == inbound.message_id),
            None,
        )
        current_message = (
            renderer.message(
                current_row,
                current_message_id=inbound.message_id,
                current_content=content,
            )
            if current_row is not None
            else ChatMessage(role="user", content=renderer.render_inbound(inbound, content))
        )
        rendered: list[tuple[int, ChatMessage]] = []
        for row in recent:
            if row.platform_message_id == inbound.message_id:
                continue
            message = renderer.message(row)
            if not (message.content or "").strip():
                continue
            rendered.append((row.id, message))
        selection = cls._select_history_window(
            tuple(rendered),
            anchor_event_id=anchor_event_id,
            high_event_limit=max(0, event_limit - 1),
            high_character_limit=max(0, character_budget - len(current_message.content or "")),
            low_watermark_ratio=low_watermark_ratio,
            fallback_anchor_event_id=current_row.id if current_row is not None else None,
        )
        return _BoundedMessages(
            history_messages=selection.messages,
            current_message=current_message,
            history_anchor_event_id=selection.anchor_event_id,
            history_window_rolled=selection.rolled,
        )

    @staticmethod
    def _select_history_window(
        rendered: tuple[tuple[int, ChatMessage], ...],
        *,
        anchor_event_id: int | None,
        high_event_limit: int,
        high_character_limit: int,
        low_watermark_ratio: float,
        fallback_anchor_event_id: int | None,
        event_weights: dict[int, int] | None = None,
    ) -> _HistoryWindowSelection:
        """Keep one prefix stable until a high watermark forces a block roll."""

        anchor_index = next(
            (index for index, item in enumerate(rendered) if item[0] == anchor_event_id),
            None,
        )
        anchor_found = anchor_index is not None
        candidate = rendered[anchor_index:] if anchor_index is not None else rendered
        candidate_characters = sum(len(item.content or "") for _, item in candidate)
        candidate_event_count = sum(
            (event_weights or {}).get(event_id, 1) for event_id, _message in candidate
        )
        must_roll = (
            not anchor_found
            or candidate_event_count > high_event_limit
            or candidate_characters > high_character_limit
        )
        if not must_roll:
            return _HistoryWindowSelection(
                messages=tuple(item for _, item in candidate),
                anchor_event_id=(candidate[0][0] if candidate else fallback_anchor_event_id),
                rolled=False,
            )

        if high_event_limit <= 0 or high_character_limit <= 0:
            return _HistoryWindowSelection(
                messages=(),
                anchor_event_id=fallback_anchor_event_id,
                rolled=anchor_event_id is not None,
            )

        low_event_limit = max(1, int(high_event_limit * low_watermark_ratio))
        low_character_limit = max(1, int(high_character_limit * low_watermark_ratio))
        selected_reversed: list[tuple[int, ChatMessage]] = []
        selected_characters = 0
        selected_events = 0
        for item in reversed(candidate):
            size = len(item[1].content or "")
            weight = (event_weights or {}).get(item[0], 1)
            if selected_events >= low_event_limit:
                break
            if selected_reversed and selected_events + weight > low_event_limit:
                break
            if not selected_reversed and size > high_character_limit:
                break
            if selected_reversed and selected_characters + size > low_character_limit:
                break
            selected_reversed.append(item)
            selected_characters += size
            selected_events += weight
        selected = tuple(reversed(selected_reversed))
        return _HistoryWindowSelection(
            messages=tuple(item for _, item in selected),
            anchor_event_id=(selected[0][0] if selected else fallback_anchor_event_id),
            rolled=anchor_event_id is not None,
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
                "occurred_at": row.occurred_at.isoformat(),
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
    ) -> _BoundedMessages:
        renderer = ChatEventPromptRenderer(recent)
        trigger = renderer.render_event(current_event)
        rendered: list[tuple[int, ChatMessage]] = []
        for row in recent:
            if row.id == current_event.id:
                continue
            message = renderer.message(row)
            if not message.content:
                continue
            rendered.append((row.id, message))
        selection = cls._select_history_window(
            tuple(rendered),
            anchor_event_id=anchor_event_id,
            high_event_limit=max(0, event_limit - 1),
            high_character_limit=max(0, character_budget - len(trigger)),
            low_watermark_ratio=low_watermark_ratio,
            fallback_anchor_event_id=current_event.id,
        )
        return _BoundedMessages(
            history_messages=selection.messages,
            current_message=ChatMessage(role="system", content=trigger),
            history_anchor_event_id=selection.anchor_event_id,
            history_window_rolled=selection.rolled,
        )
