"""Repositories for the permanent event ledger and event state."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.eligibility import MemoryEventEligibilityPolicy
from qq_ai_bot.memory.rebuild.models import (
    MemoryRebuildPlanStatistics,
    MemoryRebuildSelection,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    AgentActionModel,
    ChatEventModel,
    MemoryJobModel,
    PersonModel,
    ProcessedEventModel,
)
from qq_ai_bot.persistence.repository_helpers import _event_record
from qq_ai_bot.persistence.repository_records import (
    EventRecord,
)
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


def _scope_conditions(scope: ConversationScope) -> tuple[Any, ...]:
    conditions: list[Any] = [ChatEventModel.bot_user_id == scope.bot_user_id]
    if scope.scope_type is ScopeType.GROUP:
        conditions.extend(
            (
                ChatEventModel.scope_type == ScopeType.GROUP.value,
                ChatEventModel.group_id == scope.group_id,
            )
        )
    else:
        conditions.extend(
            (
                ChatEventModel.scope_type == ScopeType.PRIVATE.value,
                ChatEventModel.private_peer_user_id == scope.private_peer_user_id,
            )
        )
    return tuple(conditions)


class EventLedgerRepository:
    """Append, query, search, and forget permanent raw chat events."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._memory_eligibility = MemoryEventEligibilityPolicy()
        self._writer = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())

    def set_scoped_writer(self, writer: ScopedEventLedgerUnitOfWork) -> None:
        self._writer = writer

    async def maximum_event_id(self) -> int:
        async with self._database.sessions() as session:
            return int(await session.scalar(select(func.max(ChatEventModel.id))) or 0)

    def _rebuild_conditions(
        self,
        selection: MemoryRebuildSelection,
        *,
        snapshot_max_event_id: int,
        eligible_only: bool,
    ) -> tuple[Any, ...]:
        conditions: list[Any] = [ChatEventModel.id <= snapshot_max_event_id]
        if selection.bot_user_ids:
            conditions.append(ChatEventModel.bot_user_id.in_(selection.bot_user_ids))
        if selection.scope_types:
            conditions.append(
                ChatEventModel.scope_type.in_(tuple(scope.value for scope in selection.scope_types))
            )
        if selection.sender_user_ids:
            conditions.append(ChatEventModel.sender_user_id.in_(selection.sender_user_ids))
        if selection.group_ids:
            conditions.append(ChatEventModel.group_id.in_(selection.group_ids))
        if selection.after is not None:
            conditions.append(ChatEventModel.occurred_at >= selection.after)
        if selection.before is not None:
            conditions.append(ChatEventModel.occurred_at <= selection.before)
        if selection.minimum_event_id is not None:
            conditions.append(ChatEventModel.id >= selection.minimum_event_id)
        if selection.maximum_event_id is not None:
            conditions.append(ChatEventModel.id <= selection.maximum_event_id)
        if eligible_only:
            conditions.extend(
                self._memory_eligibility.sql_conditions(
                    include_failed_live_jobs=selection.include_failed_live_jobs
                )
            )
        return tuple(conditions)

    async def count_rebuild_candidates(
        self,
        selection: MemoryRebuildSelection,
        *,
        snapshot_max_event_id: int,
    ) -> MemoryRebuildPlanStatistics:
        base = self._rebuild_conditions(
            selection,
            snapshot_max_event_id=snapshot_max_event_id,
            eligible_only=False,
        )
        eligible = self._rebuild_conditions(
            selection,
            snapshot_max_event_id=snapshot_max_event_id,
            eligible_only=True,
        )
        candidate = (
            select(
                ChatEventModel.id,
                ChatEventModel.scope_type,
                ChatEventModel.content,
                ChatEventModel.occurred_at,
            )
            .where(*eligible)
            .order_by(ChatEventModel.occurred_at.asc(), ChatEventModel.id.asc())
        )
        if selection.maximum_events is not None:
            candidate = candidate.limit(selection.maximum_events)
        candidate_rows = candidate.subquery()
        async with self._database.sessions() as session:
            matched = int(
                await session.scalar(select(func.count()).select_from(ChatEventModel).where(*base))
                or 0
            )
            summary = (
                await session.execute(
                    select(
                        func.count(candidate_rows.c.id),
                        func.sum(func.length(candidate_rows.c.content)),
                        func.sum(func.iif(candidate_rows.c.scope_type == "private", 1, 0)),
                        func.sum(func.iif(candidate_rows.c.scope_type == "group", 1, 0)),
                        func.min(candidate_rows.c.occurred_at),
                        func.max(candidate_rows.c.occurred_at),
                    )
                )
            ).one()
            status_result = (
                await session.execute(
                    select(MemoryJobModel.status, func.count())
                    .join(ChatEventModel, ChatEventModel.id == MemoryJobModel.event_id)
                    .where(*base)
                    .group_by(MemoryJobModel.status)
                )
            ).all()
            status_rows: dict[str, int] = {
                str(status): int(count) for status, count in status_result
            }
        eligible_count = int(summary[0] or 0)
        return MemoryRebuildPlanStatistics(
            matched_events=matched,
            eligible_events=eligible_count,
            already_processed=int(status_rows.get("done", 0)),
            live_pending_processing=int(status_rows.get("pending", 0))
            + int(status_rows.get("processing", 0)),
            failed_live_jobs=int(status_rows.get("failed", 0)),
            private_events=int(summary[2] or 0),
            group_events=int(summary[3] or 0),
            input_characters=int(summary[1] or 0),
            earliest_event=summary[4],
            latest_event=summary[5],
            estimated_extraction_requests=eligible_count,
        )

    async def list_rebuild_candidates(
        self,
        selection: MemoryRebuildSelection,
        *,
        snapshot_max_event_id: int,
        after_occurred_at: datetime | None,
        after_event_id: int | None,
        limit: int,
    ) -> tuple[EventRecord, ...]:
        conditions = list(
            self._rebuild_conditions(
                selection,
                snapshot_max_event_id=snapshot_max_event_id,
                eligible_only=True,
            )
        )
        if after_occurred_at is not None and after_event_id is not None:
            conditions.append(
                or_(
                    ChatEventModel.occurred_at > after_occurred_at,
                    and_(
                        ChatEventModel.occurred_at == after_occurred_at,
                        ChatEventModel.id > after_event_id,
                    ),
                )
            )
        bounded_limit = limit
        if selection.maximum_events is not None:
            bounded_limit = min(limit, selection.maximum_events)
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(ChatEventModel)
                    .where(*conditions)
                    .order_by(ChatEventModel.occurred_at.asc(), ChatEventModel.id.asc())
                    .limit(bounded_limit)
                )
            ).all()
        return tuple(_event_record(row) for row in rows)

    async def append(
        self,
        *,
        bot_user_id: str,
        platform_message_id: str,
        scope_type: ScopeType,
        sender_user_id: str,
        direction: str,
        content: str,
        segments: tuple[dict[str, Any], ...] = (),
        group_id: str | None = None,
        private_peer_user_id: str | None = None,
        reply_to_message_id: str | None = None,
        occurred_at: datetime | None = None,
        sender_nickname: str = "",
        sender_group_card: str = "",
        sender_is_bot: bool = False,
        origin: str = "user_message",
        automation_id: int | None = None,
        automation_run_id: int | None = None,
    ) -> tuple[EventRecord, bool]:
        """Insert idempotently and return the existing row on duplicate."""

        scope = (
            ConversationScope.group(bot_user_id, group_id or "")
            if scope_type is ScopeType.GROUP
            else ConversationScope.private(bot_user_id, private_peer_user_id or sender_user_id)
        )
        result = await self._writer.append(
            scope=scope,
            platform_message_id=platform_message_id,
            sender_user_id=sender_user_id,
            direction=direction,
            content=content,
            segments=segments,
            reply_to_message_id=reply_to_message_id,
            occurred_at=occurred_at,
            sender_nickname=sender_nickname,
            sender_group_card=sender_group_card,
            sender_is_bot=sender_is_bot,
            origin=origin,
            automation_id=automation_id,
            automation_run_id=automation_run_id,
        )
        return result.event, result.created

    async def append_inbound(
        self, message: InboundMessage, *, bot_user_id: str
    ) -> tuple[EventRecord, bool]:
        scoped_message = message
        if message.bot_user_id != bot_user_id:
            scoped_message = replace(message, bot_user_id=bot_user_id)
        result = await self._writer.append_inbound(scoped_message)
        return result.event, result.created

    async def find_by_platform_message(
        self,
        *,
        bot_user_id: str,
        platform_message_id: str,
    ) -> EventRecord | None:
        """Return one exact locally observed event without widening its conversation scope."""

        async with self._database.sessions() as session:
            row = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == bot_user_id,
                    ChatEventModel.platform_message_id == platform_message_id,
                )
            )
        return _event_record(row) if row is not None else None

    async def get_event(self, event_id: int) -> EventRecord | None:
        """Return one exact ledger event by its internal immutable identifier."""

        async with self._database.sessions() as session:
            row = await session.get(ChatEventModel, event_id)
        return _event_record(row) if row is not None else None

    async def list_scope_recent(
        self,
        scope: ConversationScope,
        *,
        after_event_id: int = 0,
        limit: int,
    ) -> tuple[EventRecord, ...]:
        """Read the newest events from one exact bot-aware scope."""

        query = select(ChatEventModel).where(
            *_scope_conditions(scope),
            ChatEventModel.id > max(0, after_event_id),
        )
        async with self._database.sessions() as session:
            rows = list(
                (await session.scalars(query.order_by(ChatEventModel.id.desc()).limit(limit))).all()
            )
        rows.reverse()
        return tuple(_event_record(row) for row in rows)

    async def list_scope_before(
        self,
        scope: ConversationScope,
        *,
        before_event_id: int,
        limit: int,
    ) -> tuple[EventRecord, ...]:
        """Return the bounded scope prefix preceding one ledger event id."""

        query = select(ChatEventModel).where(
            *_scope_conditions(scope),
            ChatEventModel.id < before_event_id,
        )
        async with self._database.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        query.order_by(ChatEventModel.id.desc()).limit(max(1, limit))
                    )
                ).all()
            )
        rows.reverse()
        return tuple(_event_record(row) for row in rows)

    async def list_scope_after(
        self,
        scope: ConversationScope,
        *,
        after_event_id: int,
        through_event_id: int | None = None,
        limit: int,
    ) -> tuple[EventRecord, ...]:
        query = select(ChatEventModel).where(
            *_scope_conditions(scope),
            ChatEventModel.id > after_event_id,
        )
        if through_event_id is not None:
            query = query.where(ChatEventModel.id <= through_event_id)
        async with self._database.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        query.order_by(ChatEventModel.id.asc()).limit(max(1, limit))
                    )
                ).all()
            )
        return tuple(_event_record(row) for row in rows)

    async def count_scope_range(
        self,
        scope: ConversationScope,
        *,
        after_event_id: int,
        through_event_id: int,
    ) -> int:
        async with self._database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count(ChatEventModel.id)).where(
                        *_scope_conditions(scope),
                        ChatEventModel.id > after_event_id,
                        ChatEventModel.id <= through_event_id,
                    )
                )
                or 0
            )

    async def maximum_scope_event_id(self, scope: ConversationScope) -> int:
        async with self._database.sessions() as session:
            return int(
                await session.scalar(
                    select(func.max(ChatEventModel.id)).where(*_scope_conditions(scope))
                )
                or 0
            )

    async def list_scope_around(
        self,
        scope: ConversationScope,
        *,
        event_id: int | None,
        platform_message_id: str | None,
        before: int,
        after: int,
    ) -> tuple[EventRecord | None, tuple[EventRecord, ...], tuple[EventRecord, ...]]:
        """Read nearby events strictly inside the current scope generation."""

        center: EventRecord | None = None
        if event_id is not None:
            center = await self.get_event(event_id)
        elif platform_message_id:
            center = await self.find_by_platform_message(
                bot_user_id=scope.bot_user_id,
                platform_message_id=platform_message_id,
            )
        if center is None:
            return None, (), ()
        center_scope = (
            ConversationScope.group(center.bot_user_id, center.group_id or "")
            if center.scope_type is ScopeType.GROUP
            else ConversationScope.private(center.bot_user_id, center.private_peer_user_id or "")
        )
        if center_scope != scope:
            return None, (), ()
        async with self._database.sessions() as session:
            starts_after = await session.scalar(
                select(ConversationScopeModel.starts_after_event_id).where(
                    ConversationScopeModel.scope_key == scope.key
                )
            )
        boundary = int(starts_after or 0)
        if center.id <= boundary:
            return None, (), ()
        earlier = (
            tuple(
                row
                for row in await self.list_scope_before(
                    scope, before_event_id=center.id, limit=before
                )
                if row.id > boundary
            )
            if before > 0
            else ()
        )
        later = (
            await self.list_scope_after(
                scope,
                after_event_id=center.id,
                limit=after,
            )
            if after > 0
            else ()
        )
        return center, earlier, later

    async def hydrate_rebuild_subjects(self, event: EventRecord) -> EventRecord:
        """Recover only deterministic legacy mention/reply metadata in the exact conversation."""

        if event.scope_type is not ScopeType.GROUP or not event.group_id:
            return replace(event, mentioned_user_ids=(), reply_sender_user_id=None)
        mentions = list(event.mentioned_user_ids)
        if not mentions:
            for segment in event.segments:
                if segment.get("type") != "at" or not isinstance(segment.get("data"), dict):
                    continue
                raw = str(segment["data"].get("qq", "")).strip()
                if raw.isdigit():
                    mentions.append(raw)
        reply_sender = event.reply_sender_user_id
        if reply_sender is None and event.reply_to_message_id:
            referenced = await self.find_by_platform_message(
                bot_user_id=event.bot_user_id,
                platform_message_id=event.reply_to_message_id,
            )
            if (
                referenced is not None
                and referenced.scope_type is ScopeType.GROUP
                and referenced.group_id == event.group_id
                and referenced.sender_user_id != event.bot_user_id
            ):
                reply_sender = referenced.sender_user_id
        blocked = {"", event.sender_user_id, event.bot_user_id}
        return replace(
            event,
            mentioned_user_ids=tuple(
                dict.fromkeys(user_id for user_id in mentions if user_id not in blocked)
            ),
            reply_sender_user_id=(
                reply_sender if reply_sender is not None and reply_sender not in blocked else None
            ),
        )

    async def memory_job_status(self, event_id: int) -> str | None:
        async with self._database.sessions() as session:
            return cast(
                str | None,
                await session.scalar(
                    select(MemoryJobModel.status).where(MemoryJobModel.event_id == event_id)
                ),
            )

    async def sender_is_bot(self, user_id: str) -> bool:
        async with self._database.sessions() as session:
            return bool(
                await session.scalar(
                    select(PersonModel.is_bot).where(PersonModel.user_id == user_id)
                )
            )

    async def search(
        self,
        *,
        keyword: str,
        limit: int = 20,
        user_id: str | None = None,
        group_id: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
    ) -> tuple[EventRecord, ...]:
        """Search with trigram FTS, falling back to bounded LIKE for short terms."""

        bounded_limit = max(1, min(limit, 100))
        conditions: list[str] = []
        params: dict[str, Any] = {"limit": bounded_limit}
        if user_id:
            conditions.append(
                "(ce.sender_user_id = :user_id OR ce.private_peer_user_id = :user_id)"
            )
            params["user_id"] = user_id
        if group_id:
            conditions.append("ce.group_id = :group_id")
            params["group_id"] = group_id
        if after:
            conditions.append("ce.occurred_at >= :after")
            params["after"] = after
        if before:
            conditions.append("ce.occurred_at <= :before")
            params["before"] = before
        prefix = f" AND {' AND '.join(conditions)}" if conditions else ""
        stripped = keyword.strip()
        if len(stripped) >= 3:
            sql = text(
                """
                SELECT ce.* FROM chat_events AS ce
                JOIN chat_events_fts AS fts ON fts.rowid = ce.id
                WHERE chat_events_fts MATCH :keyword
                """
                + prefix
                + " ORDER BY ce.occurred_at DESC, ce.id DESC LIMIT :limit"
            )
            params["keyword"] = '"' + stripped.replace('"', '""') + '"'
        else:
            if not conditions:
                raise ValueError("short history searches require a QQ, group, or time bound")
            sql = text(
                "SELECT ce.* FROM chat_events AS ce WHERE ce.content LIKE :pattern"
                + prefix
                + " ORDER BY ce.occurred_at DESC, ce.id DESC LIMIT :limit"
            )
            params["pattern"] = f"%{stripped}%"
        async with self._database.sessions() as session:
            mappings = (await session.execute(sql, params)).mappings().all()
        records: list[EventRecord] = []
        for row in mappings:
            raw_occurred = row["occurred_at"]
            occurred = (
                datetime.fromisoformat(raw_occurred)
                if isinstance(raw_occurred, str)
                else raw_occurred
            )
            raw_segments = json.loads(str(row["segments_json"]))
            records.append(
                EventRecord(
                    id=int(row["id"]),
                    bot_user_id=str(row["bot_user_id"]),
                    platform_message_id=str(row["platform_message_id"]),
                    scope_type=ScopeType(str(row["scope_type"])),
                    sender_user_id=str(row["sender_user_id"]),
                    sender_nickname=str(row["sender_nickname"] or ""),
                    sender_group_card=str(row["sender_group_card"] or ""),
                    direction=str(row["direction"]),
                    content=str(row["content"]),
                    visual_summary=str(row["visual_summary"] or ""),
                    segments=tuple(raw_segments) if isinstance(raw_segments, list) else (),
                    occurred_at=occurred,
                    group_id=row["group_id"],
                    private_peer_user_id=row["private_peer_user_id"],
                    reply_to_message_id=row["reply_to_message_id"],
                    origin=str(row["origin"] or "user_message"),
                    automation_id=row["automation_id"],
                    automation_run_id=row["automation_run_id"],
                )
            )
        return tuple(reversed(records))

    async def set_visual_summary(self, event_id: int, summary: str) -> bool:
        """Attach one compact derived observation to its immutable source event."""

        return await self._writer.set_visual_summary(event_id, summary)


class AgentActionRepository:
    """Record safe metadata for generic OneBot actions."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record(
        self,
        *,
        actor_user_id: str | None,
        action: str,
        success: bool,
        duration_seconds: float,
        error_category: str | None = None,
    ) -> None:
        async with self._database.sessions() as session, session.begin():
            session.add(
                AgentActionModel(
                    actor_user_id=actor_user_id,
                    action=action[:128],
                    success=success,
                    duration_seconds=duration_seconds,
                    error_category=error_category[:64] if error_category else None,
                    created_at=datetime.now(UTC),
                )
            )


class ProcessedEventRepository:
    """Durable idempotency repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def claim(self, event_key: str, *, expires_at: datetime) -> bool:
        try:
            async with self._database.sessions() as session, session.begin():
                session.add(
                    ProcessedEventModel(
                        event_key=event_key,
                        processed_at=datetime.now(UTC),
                        expires_at=expires_at,
                    )
                )
            return True
        except IntegrityError:
            return False

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(ProcessedEventModel).where(ProcessedEventModel.expires_at <= cutoff)
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)
