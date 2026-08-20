"""Build local autonomous-admission features from ledger projections.

This is the production replacement for leftover ``PlannerContextBuilder.admission_features``.
It never calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.conversation.participation import AdmissionFeatures, AdmissionSignalHint
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.persistence.repositories import EventLedgerRepository, RelationshipRepository
from qq_ai_bot.persistence.repository_records import EventRecord

_HISTORY_LIMIT = 10


@dataclass(frozen=True, slots=True)
class _ConversationMetrics:
    pending: int
    bot_count: int
    average_interval: float
    idle: float
    since_bot: float | None
    last_was_bot: bool


class AdmissionFeatureBuilder:
    """Keep repository reads out of the pure participation scorer."""

    def __init__(
        self,
        *,
        ledger: EventLedgerRepository,
        relationships: RelationshipRepository,
    ) -> None:
        self._ledger = ledger
        self._relationships = relationships

    async def admission_features(
        self,
        *,
        inbound: InboundMessage,
        content: str,
        runtime: RuntimeConfigSnapshot,
        plugin_signals: tuple[AdmissionSignalHint, ...] = (),
        now: datetime | None = None,
    ) -> AdmissionFeatures:
        """Build local participation features without a generative model call."""

        del runtime
        current_time = now or datetime.now(UTC)
        recent = await self._ledger.list_scope_recent(
            inbound.scope(),
            limit=_HISTORY_LIMIT + 1,
        )
        relationship = await self._relationships.get(inbound.sender.user_id)
        metrics = self._metrics(recent, inbound.bot_user_id, current_time)
        relationship_adjustment = 0.0
        if relationship is not None:
            relationship_adjustment = max(
                -5.0,
                min(5.0, (relationship.relationship_weight - 50) / 10),
            )
        return AdmissionFeatures(
            scope_type=inbound.scope_type,
            text=content,
            reply_target_is_bot=(
                bool(inbound.reply_sender_user_id)
                and inbound.reply_sender_user_id == inbound.bot_user_id
            ),
            mentions_bot=inbound.mentions_bot,
            continuation=metrics.last_was_bot,
            pending_message_count=metrics.pending,
            recent_bot_messages=metrics.bot_count,
            recent_total_messages=len(recent),
            average_human_interval_seconds=metrics.average_interval,
            idle_seconds=metrics.idle,
            seconds_since_last_bot_message=metrics.since_bot,
            relationship_adjustment=relationship_adjustment,
            plugin_signals=plugin_signals,
            new_message_count=max(1, metrics.pending),
            media_only=not content.strip() and bool(inbound.attachments),
            now=current_time,
        )

    @staticmethod
    def _metrics(
        rows: tuple[EventRecord, ...],
        bot_user_id: str,
        now: datetime,
    ) -> _ConversationMetrics:
        human = [
            row for row in rows if row.event_kind == "message" and row.sender_user_id != bot_user_id
        ]
        bot = [
            row for row in rows if row.event_kind == "message" and row.sender_user_id == bot_user_id
        ]
        messages = [row for row in rows if row.event_kind == "message"]
        normalized_now = AdmissionFeatureBuilder._aware_utc(now)
        intervals = [
            max(
                0.0,
                (
                    AdmissionFeatureBuilder._aware_utc(right.occurred_at)
                    - AdmissionFeatureBuilder._aware_utc(left.occurred_at)
                ).total_seconds(),
            )
            for left, right in pairwise(human)
        ]
        last_bot_index = max(
            (
                index
                for index, row in enumerate(rows)
                if row.event_kind == "message" and row.sender_user_id == bot_user_id
            ),
            default=-1,
        )
        pending = sum(
            1
            for row in rows[last_bot_index + 1 :]
            if row.event_kind == "message" and row.sender_user_id != bot_user_id
        )
        last_time = (
            AdmissionFeatureBuilder._aware_utc(messages[-1].occurred_at)
            if messages
            else normalized_now
        )
        last_bot_time = AdmissionFeatureBuilder._aware_utc(bot[-1].occurred_at) if bot else None
        return _ConversationMetrics(
            pending=pending,
            bot_count=len(bot),
            average_interval=sum(intervals) / len(intervals) if intervals else 60.0,
            idle=max(0.0, (normalized_now - last_time).total_seconds()),
            since_bot=(
                max(0.0, (normalized_now - last_bot_time).total_seconds())
                if last_bot_time is not None
                else None
            ),
            last_was_bot=bool(messages and messages[-1].sender_user_id == bot_user_id),
        )

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
