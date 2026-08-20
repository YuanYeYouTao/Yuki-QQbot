"""The only runtime write path for the permanent conversation event ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.rollup.db_models import (
    ConversationRollupJobModel,
    ConversationRollupModel,
)
from qq_ai_bot.conversation.rollup.metrics import ConversationRollupMetrics
from qq_ai_bot.conversation.rollup.models import ConversationScopeState, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.prompt_accounting import (
    prompt_accounting_event_characters,
)
from qq_ai_bot.conversation.rollup.repository import (
    _scope_from_row,
    _scope_state,
    eligible_prefix,
    exceeds_high_watermark,
    get_or_create_scope_row,
    recount_scope_uncovered,
)
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _ensure_group, _ensure_person, _event_record
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class ScopedAppendResult:
    event: EventRecord
    scope: ConversationScopeState
    created: bool
    job_signalled: bool


@dataclass(frozen=True, slots=True)
class NewGenerationResult:
    event: EventRecord
    scope: ConversationScopeState
    generation_changed: bool


class ScopedEventLedgerUnitOfWork:
    """Commit event, scope counters, and the single job signal atomically."""

    def __init__(
        self,
        database: Database,
        *,
        config: RollupPolicyConfig,
        notify_worker: Callable[[], None] | None = None,
        metrics: ConversationRollupMetrics | None = None,
    ) -> None:
        self._database = database
        self._config = config
        self._notify_worker = notify_worker
        self.metrics = metrics or ConversationRollupMetrics()

    def set_worker_notifier(self, notify_worker: Callable[[], None] | None) -> None:
        self._notify_worker = notify_worker

    async def append_inbound(self, message: InboundMessage) -> ScopedAppendResult:
        scope = message.scope()
        segments = list(message.segments)
        segments.append(
            {
                "type": "yuki_context",
                "data": {
                    "mentioned_user_ids": list(message.mentioned_user_ids),
                    "reply_sender_user_id": message.reply_sender_user_id,
                },
            }
        )
        return await self.append(
            scope=scope,
            platform_message_id=message.message_id,
            sender_user_id=message.sender.user_id,
            direction="inbound",
            content=message.text,
            segments=tuple(segments),
            reply_to_message_id=message.reply_to_message_id,
            occurred_at=message.received_at,
            sender_nickname=message.sender.nickname,
            sender_group_card=message.sender.group_card,
            sender_is_bot=message.sender.is_bot,
        )

    async def append_external(
        self,
        *,
        scope: ConversationScope,
        platform_message_id: str,
        source_plugin_id: str,
        external_source: str,
        external_event_key: str,
        external_event_type: str,
        external_payload: dict[str, Any],
        external_target_id: str,
        content: str,
        occurred_at: datetime,
    ) -> ScopedAppendResult:
        """Persist one plugin/automation event through the scoped ledger boundary."""

        return await self.append(
            scope=scope,
            platform_message_id=platform_message_id,
            sender_user_id=scope.bot_user_id,
            direction="external",
            content=content,
            occurred_at=occurred_at,
            sender_is_bot=True,
            origin="plugin_background",
            event_kind="external_event",
            source_plugin_id=source_plugin_id,
            external_source=external_source,
            external_event_key=external_event_key,
            external_event_type=external_event_type,
            external_payload=external_payload,
            external_target_id=external_target_id,
        )

    async def append(
        self,
        *,
        scope: ConversationScope,
        platform_message_id: str,
        sender_user_id: str,
        direction: str,
        content: str,
        segments: tuple[dict[str, Any], ...] = (),
        reply_to_message_id: str | None = None,
        occurred_at: datetime | None = None,
        sender_nickname: str = "",
        sender_group_card: str = "",
        sender_is_bot: bool = False,
        origin: str = "user_message",
        automation_id: int | None = None,
        automation_run_id: int | None = None,
        event_kind: str = "message",
        source_plugin_id: str | None = None,
        external_source: str | None = None,
        external_event_key: str | None = None,
        external_event_type: str | None = None,
        external_payload: dict[str, Any] | None = None,
        external_target_id: str | None = None,
    ) -> ScopedAppendResult:
        timestamp = occurred_at or datetime.now(UTC)
        observed_at = datetime.now(UTC)
        signalled = False
        async with self._database.immediate_session() as session:
            existing = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == scope.bot_user_id,
                    ChatEventModel.platform_message_id == platform_message_id,
                )
            )
            if existing is not None:
                if self._scope_for_event(_event_record(existing)) != scope:
                    raise RuntimeError("platform message id already belongs to another scope")
                scope_row = await get_or_create_scope_row(
                    session, scope, first_event_id=existing.id, now=observed_at
                )
                return ScopedAppendResult(
                    event=_event_record(existing),
                    scope=_scope_state(scope_row),
                    created=False,
                    job_signalled=False,
                )
            await self._ensure_identities(
                session,
                scope=scope,
                sender_user_id=sender_user_id,
                sender_nickname=sender_nickname,
                sender_is_bot=sender_is_bot,
                timestamp=timestamp,
                observed_at=observed_at,
            )
            row = ChatEventModel(
                bot_user_id=scope.bot_user_id,
                platform_message_id=platform_message_id,
                scope_type=scope.scope_type.value,
                group_id=scope.group_id,
                private_peer_user_id=scope.private_peer_user_id,
                sender_user_id=sender_user_id,
                sender_nickname=sender_nickname[:128],
                sender_group_card=sender_group_card[:128],
                direction=direction,
                event_kind=event_kind,
                source_plugin_id=source_plugin_id,
                external_source=external_source,
                external_event_key=external_event_key,
                external_event_type=external_event_type,
                external_payload_json=(
                    json.dumps(external_payload, ensure_ascii=False, separators=(",", ":"))
                    if external_payload is not None
                    else None
                ),
                external_target_id=external_target_id,
                content=content,
                visual_summary="",
                segments_json=json.dumps(segments, ensure_ascii=False, separators=(",", ":")),
                reply_to_message_id=reply_to_message_id,
                origin=origin[:32],
                automation_id=automation_id,
                automation_run_id=automation_run_id,
                occurred_at=timestamp,
                observed_at=observed_at,
            )
            session.add(row)
            await session.flush()
            event = _event_record(row)
            scope_row = await get_or_create_scope_row(
                session, scope, first_event_id=row.id, now=observed_at
            )
            scope_row.last_event_id = max(scope_row.last_event_id, row.id)
            scope_row.uncovered_event_count += 1
            scope_row.uncovered_character_count += prompt_accounting_event_characters(
                event,
                events=(event,),
                bot_display_name=self._config.bot_display_name,
                timezone=self._config.timezone,
            )
            scope_row.updated_at = observed_at
            signalled = await self._signal_if_needed(session, scope_row, force_existing=True)
            state = _scope_state(scope_row)
        self._notify_after_commit(signalled)
        return ScopedAppendResult(event=event, scope=state, created=True, job_signalled=signalled)

    async def append_new_generation_command(
        self,
        *,
        scope: ConversationScope,
        inbound: InboundMessage,
    ) -> NewGenerationResult:
        """Append `/ai new` and switch generation in the same short transaction."""

        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            await self._ensure_identities(
                session,
                scope=scope,
                sender_user_id=inbound.sender.user_id,
                sender_nickname=inbound.sender.nickname,
                sender_is_bot=inbound.sender.is_bot,
                timestamp=inbound.received_at,
                observed_at=now,
            )
            row = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == scope.bot_user_id,
                    ChatEventModel.platform_message_id == inbound.message_id,
                )
            )
            if row is None:
                row = ChatEventModel(
                    bot_user_id=scope.bot_user_id,
                    platform_message_id=inbound.message_id,
                    scope_type=scope.scope_type.value,
                    group_id=scope.group_id,
                    private_peer_user_id=scope.private_peer_user_id,
                    sender_user_id=inbound.sender.user_id,
                    sender_nickname=inbound.sender.nickname[:128],
                    sender_group_card=inbound.sender.group_card[:128],
                    direction="inbound",
                    event_kind="message",
                    content=inbound.text,
                    visual_summary="",
                    segments_json=json.dumps(
                        inbound.segments, ensure_ascii=False, separators=(",", ":")
                    ),
                    reply_to_message_id=inbound.reply_to_message_id,
                    origin="user_message",
                    occurred_at=inbound.received_at,
                    observed_at=now,
                )
                session.add(row)
                await session.flush()
            scope_row = await get_or_create_scope_row(
                session, scope, first_event_id=row.id, now=now
            )
            changed = scope_row.last_generation_change_event_id != row.id
            if changed:
                scope_row.generation += 1
                scope_row.starts_after_event_id = row.id
                scope_row.last_generation_change_event_id = row.id
                scope_row.last_event_id = max(scope_row.last_event_id, row.id)
                scope_row.uncovered_event_count = 0
                scope_row.uncovered_character_count = 0
                scope_row.updated_at = now
                await session.execute(
                    delete(ConversationRollupModel).where(
                        ConversationRollupModel.scope_id == scope_row.id
                    )
                )
                await session.execute(
                    delete(ConversationRollupJobModel).where(
                        ConversationRollupJobModel.scope_id == scope_row.id
                    )
                )
            state = _scope_state(scope_row)
            event = _event_record(row)
        return NewGenerationResult(event=event, scope=state, generation_changed=changed)

    async def set_visual_summary(self, event_id: int, summary: str) -> bool:
        normalized = summary.strip()[:6000]
        lowered = normalized.casefold()
        if "data:image/" in lowered or "base64://" in lowered:
            raise ValueError("visual_summary must not contain image or Base64 payloads")
        now = datetime.now(UTC)
        signalled = False
        async with self._database.immediate_session() as session:
            row = await session.get(ChatEventModel, event_id)
            if row is None:
                return False
            old = _event_record(row)
            scope = self._scope_for_event(old)
            scope_row = await get_or_create_scope_row(
                session, scope, first_event_id=row.id, now=now
            )
            old_characters = prompt_accounting_event_characters(
                old,
                events=(old,),
                bot_display_name=self._config.bot_display_name,
                timezone=self._config.timezone,
            )
            row.visual_summary = normalized
            await session.flush()
            new = _event_record(row)
            rollup = await session.get(ConversationRollupModel, scope_row.id)
            coverage = (
                rollup.covered_through_event_id
                if rollup is not None and rollup.generation == scope_row.generation
                else scope_row.starts_after_event_id
            )
            if row.id > coverage:
                scope_row.uncovered_character_count += (
                    prompt_accounting_event_characters(
                        new,
                        events=(new,),
                        bot_display_name=self._config.bot_display_name,
                        timezone=self._config.timezone,
                    )
                    - old_characters
                )
                if scope_row.uncovered_character_count < 0:
                    await recount_scope_uncovered(session, scope_row, self._config)
                    self.metrics.counter_repairs += 1
                    if scope_row.uncovered_character_count < 0:
                        self.metrics.counter_reconcile_failures += 1
                        raise RuntimeError("visual projection counter recount failed")
                scope_row.updated_at = now
                signalled = await self._signal_if_needed(session, scope_row, force_existing=True)
            else:
                self.metrics.late_visual_after_coverage += 1
        self._notify_after_commit(signalled)
        return True

    async def _signal_if_needed(
        self,
        session: AsyncSession,
        scope_row: Any,
        *,
        force_existing: bool,
    ) -> bool:
        job = await session.get(ConversationRollupJobModel, scope_row.id)
        now = datetime.now(UTC)
        if job is not None:
            if force_existing:
                job.signal_revision += 1
                job.updated_at = now
                return True
            return False
        scope = _scope_from_row(scope_row)
        rollup = await session.get(ConversationRollupModel, scope_row.id)
        coverage = (
            rollup.covered_through_event_id
            if rollup is not None and rollup.generation == scope_row.generation
            else scope_row.starts_after_event_id
        )
        rows = tuple(
            (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.bot_user_id == scope.bot_user_id,
                        ChatEventModel.scope_type == scope.scope_type.value,
                        ChatEventModel.group_id == scope.group_id,
                        ChatEventModel.private_peer_user_id == scope.private_peer_user_id,
                        ChatEventModel.id > coverage,
                        ChatEventModel.id <= scope_row.last_event_id,
                    )
                    .order_by(ChatEventModel.id.asc())
                )
            ).all()
        )
        events = tuple(_event_record(row) for row in rows)
        if not exceeds_high_watermark(eligible_prefix(events, self._config), self._config):
            return False
        session.add(
            ConversationRollupJobModel(
                scope_id=scope_row.id,
                generation=scope_row.generation,
                signal_revision=1,
                status="pending",
                failure_count=0,
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                next_attempt_at=now,
                last_error_category=None,
                created_at=now,
                updated_at=now,
            )
        )
        return True

    @staticmethod
    async def _ensure_identities(
        session: AsyncSession,
        *,
        scope: ConversationScope,
        sender_user_id: str,
        sender_nickname: str,
        sender_is_bot: bool,
        timestamp: datetime,
        observed_at: datetime,
    ) -> None:
        await _ensure_person(
            session,
            sender_user_id,
            nickname=sender_nickname,
            is_bot=sender_is_bot,
            now=timestamp,
        )
        await _ensure_person(session, scope.bot_user_id, is_bot=True, now=observed_at)
        if scope.private_peer_user_id:
            await _ensure_person(session, scope.private_peer_user_id, now=timestamp)
        if scope.group_id:
            await _ensure_group(session, scope.group_id, now=timestamp)

    @staticmethod
    def _scope_for_event(event: EventRecord) -> ConversationScope:
        if event.scope_type is ScopeType.GROUP:
            return ConversationScope.group(event.bot_user_id, event.group_id or "")
        return ConversationScope.private(
            event.bot_user_id, event.private_peer_user_id or event.sender_user_id
        )

    def _notify_after_commit(self, signalled: bool) -> None:
        if signalled and self._notify_worker is not None:
            try:
                self._notify_worker()
            except Exception:
                # Durable polling is authoritative; the process-local wakeup
                # is only a latency optimization.
                return
