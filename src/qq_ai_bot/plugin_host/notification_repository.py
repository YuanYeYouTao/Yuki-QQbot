"""Transactional persistence for plugin external events, delivery, and background turns."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, GroupModel, PersonModel
from qq_ai_bot.persistence.repository_helpers import _ensure_person
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTargetGrantModel,
    PluginBackgroundTurnJobModel,
    PluginInstallationModel,
    PluginMediaArtifactModel,
    PluginNotificationOutboxModel,
)
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import (
    BackgroundTargetGrantView,
    NotificationPublishReceipt,
    NotificationTarget,
    PublishNotificationRequest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: int
    notification_id: str
    source_event_id: int
    plugin_id: str
    target_type: str
    target_id: str
    bot_user_id: str
    part_type: str
    text: str
    media_handle_id: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class BackgroundTurnJobRecord:
    id: int
    source_event_id: int
    plugin_id: str
    target_type: str
    target_id: str
    bot_user_id: str
    agent_intent: str
    attempts: int


class PluginNotificationRepository:
    """Keep publication idempotency and delivery work in Host-owned transactions."""

    def __init__(
        self,
        database: Database,
        scoped_events: ScopedEventLedgerUnitOfWork | None = None,
    ) -> None:
        self._database = database
        self._scoped_events = scoped_events or ScopedEventLedgerUnitOfWork(
            database,
            config=RollupPolicyConfig(),
        )

    async def grant_target(
        self,
        *,
        plugin_id: str,
        target: NotificationTarget,
        bot_user_id: str,
        created_by_user_id: str,
    ) -> BackgroundTargetGrantView:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            creator = await session.get(PersonModel, created_by_user_id)
            if creator is None:
                raise PluginPermissionError("grant creator is not a known person")
            if target.target_type == "group":
                group = await session.get(GroupModel, target.target_id)
                if group is None or not group.enabled:
                    raise PluginPermissionError("notification group is unknown or disabled")
            elif await session.get(PersonModel, target.target_id) is None:
                raise PluginPermissionError("notification private target is unknown")
            await _ensure_person(session, bot_user_id, is_bot=True, now=now)
            row = await session.scalar(
                select(PluginBackgroundTargetGrantModel).where(
                    PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                    PluginBackgroundTargetGrantModel.target_type == target.target_type,
                    PluginBackgroundTargetGrantModel.target_id == target.target_id,
                )
            )
            if row is None:
                row = PluginBackgroundTargetGrantModel(
                    plugin_id=plugin_id,
                    target_type=target.target_type,
                    target_id=target.target_id,
                    bot_user_id=bot_user_id,
                    enabled=True,
                    created_by_user_id=created_by_user_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.bot_user_id = bot_user_id
                row.enabled = True
                row.created_by_user_id = created_by_user_id
                row.updated_at = now
            await session.flush()
            return _grant_view(row)

    async def revoke_target(
        self,
        *,
        plugin_id: str,
        target: NotificationTarget,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.scalar(
                select(PluginBackgroundTargetGrantModel).where(
                    PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                    PluginBackgroundTargetGrantModel.target_type == target.target_type,
                    PluginBackgroundTargetGrantModel.target_id == target.target_id,
                )
            )
            if row is None:
                return False
            row.enabled = False
            row.updated_at = now
            await session.execute(
                update(PluginNotificationOutboxModel)
                .where(
                    PluginNotificationOutboxModel.plugin_id == plugin_id,
                    PluginNotificationOutboxModel.target_type == target.target_type,
                    PluginNotificationOutboxModel.target_id == target.target_id,
                    PluginNotificationOutboxModel.status.in_(("pending", "processing")),
                )
                .values(status="cancelled", lease_until=None, updated_at=now)
            )
            await session.execute(
                update(PluginBackgroundTurnJobModel)
                .where(
                    PluginBackgroundTurnJobModel.plugin_id == plugin_id,
                    PluginBackgroundTurnJobModel.target_type == target.target_type,
                    PluginBackgroundTurnJobModel.target_id == target.target_id,
                    PluginBackgroundTurnJobModel.status.in_(("pending", "processing")),
                )
                .values(status="cancelled", lease_until=None, updated_at=now)
            )
            return True

    async def list_grants(self, plugin_id: str) -> tuple[BackgroundTargetGrantView, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PluginBackgroundTargetGrantModel)
                    .where(PluginBackgroundTargetGrantModel.plugin_id == plugin_id)
                    .order_by(
                        PluginBackgroundTargetGrantModel.target_type,
                        PluginBackgroundTargetGrantModel.target_id,
                    )
                )
            ).all()
        return tuple(_grant_view(row) for row in rows)

    async def grant_creator(
        self, *, plugin_id: str, target_type: str, target_id: str
    ) -> str | None:
        async with self._database.sessions() as session:
            creator = await session.scalar(
                select(PluginBackgroundTargetGrantModel.created_by_user_id)
                .join(
                    PluginInstallationModel,
                    PluginInstallationModel.plugin_id == PluginBackgroundTargetGrantModel.plugin_id,
                )
                .where(
                    PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                    PluginBackgroundTargetGrantModel.target_type == target_type,
                    PluginBackgroundTargetGrantModel.target_id == target_id,
                    PluginBackgroundTargetGrantModel.enabled.is_(True),
                    PluginInstallationModel.enabled.is_(True),
                    PluginInstallationModel.status == "running",
                )
            )
            if creator is None:
                return None
            if target_type == "group":
                enabled = await session.scalar(
                    select(GroupModel.enabled).where(GroupModel.group_id == target_id)
                )
                if not enabled:
                    return None
            elif await session.get(PersonModel, target_id) is None:
                return None
            return creator

    async def publish(
        self,
        *,
        plugin_id: str,
        request: PublishNotificationRequest,
    ) -> NotificationPublishReceipt:
        payload_json = json.dumps(request.payload, ensure_ascii=False, separators=(",", ":"))
        if len(payload_json.encode("utf-8")) > 32 * 1024:
            raise ValueError("notification payload exceeds 32 KiB")
        for attempt in range(2):
            try:
                receipt = await self._publish_once(
                    plugin_id=plugin_id,
                    request=request,
                )
                logger.info(
                    "plugin_external_event_published plugin_id=%s event_type=%s "
                    "target_type=%s event_created=%s deduplicated=%s",
                    plugin_id,
                    request.event_type,
                    request.target.target_type,
                    receipt.event_created,
                    receipt.deduplicated,
                )
                return receipt
            except IntegrityError:
                if attempt:
                    raise
        raise AssertionError("publication retry must return")

    async def _publish_once(
        self,
        *,
        plugin_id: str,
        request: PublishNotificationRequest,
    ) -> NotificationPublishReceipt:
        now = datetime.now(UTC)
        target = request.target
        scope, bot_user_id = await self._resolve_publication_scope(
            plugin_id=plugin_id,
            target=target,
        )
        appended = await self._scoped_events.append_external(
            scope=scope,
            platform_message_id=_external_platform_id(plugin_id, request.event_key, target),
            source_plugin_id=plugin_id,
            external_source=request.external_source,
            external_event_key=request.event_key,
            external_event_type=request.event_type,
            external_payload=request.payload,
            external_target_id=target.target_id,
            content=request.summary,
            occurred_at=_aware(request.occurred_at),
        )
        async with self._database.sessions() as session, session.begin():
            installation = await session.get(PluginInstallationModel, plugin_id)
            if installation is None or not installation.enabled or installation.status != "running":
                raise PluginPermissionError("plugin is not running")
            grant = await session.scalar(
                select(PluginBackgroundTargetGrantModel).where(
                    PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                    PluginBackgroundTargetGrantModel.target_type == target.target_type,
                    PluginBackgroundTargetGrantModel.target_id == target.target_id,
                    PluginBackgroundTargetGrantModel.enabled.is_(True),
                )
            )
            if grant is None:
                raise PluginPermissionError("notification target is not granted")
            if grant.bot_user_id != bot_user_id:
                raise PluginPermissionError("notification target grant changed during publish")
            if target.target_type == "group":
                group = await session.get(GroupModel, target.target_id)
                if group is None or not group.enabled:
                    raise PluginPermissionError("notification group is unknown or disabled")
            elif await session.get(PersonModel, target.target_id) is None:
                raise PluginPermissionError("notification private target is unknown")
            notification_id = _notification_id(
                plugin_id, request.event_key, target.target_type, target.target_id
            )
            for index, handle_id in enumerate(request.media_handles):
                part_key = f"media:{index}:{handle_id}"
                existing_part = await session.scalar(
                    select(PluginNotificationOutboxModel.id).where(
                        PluginNotificationOutboxModel.notification_id == notification_id,
                        PluginNotificationOutboxModel.part_key == part_key,
                    )
                )
                if existing_part is not None:
                    continue
                artifact = await session.get(PluginMediaArtifactModel, handle_id)
                if (
                    artifact is None
                    or artifact.plugin_id != plugin_id
                    or _aware(artifact.expires_at) <= now
                ):
                    raise PluginPermissionError("media handle is invalid, expired, or foreign")
            existing = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.event_kind == "external_event",
                    ChatEventModel.source_plugin_id == plugin_id,
                    ChatEventModel.external_event_key == request.event_key,
                    ChatEventModel.scope_type == scope.scope_type.value,
                    ChatEventModel.external_target_id == target.target_id,
                )
            )
            if existing is None or existing.id != appended.event.id:
                raise RuntimeError("scoped external event could not be reloaded")
            event_created = appended.created
            delivery_enqueued = False
            for index, handle_id in enumerate(request.media_handles):
                delivery_enqueued |= await _ensure_outbox_part(
                    session,
                    notification_id=notification_id,
                    part_key=f"media:{index}:{handle_id}",
                    source_event_id=existing.id,
                    plugin_id=plugin_id,
                    grant=grant,
                    part_type="media",
                    text="",
                    media_handle_id=handle_id,
                    now=now,
                )
            if request.text:
                delivery_enqueued |= await _ensure_outbox_part(
                    session,
                    notification_id=notification_id,
                    part_key="text",
                    source_event_id=existing.id,
                    plugin_id=plugin_id,
                    grant=grant,
                    part_type="text",
                    text=request.text,
                    media_handle_id=None,
                    now=now,
                )
            job_created = False
            if request.ask_agent:
                job = await session.scalar(
                    select(PluginBackgroundTurnJobModel).where(
                        PluginBackgroundTurnJobModel.source_event_id == existing.id
                    )
                )
                if job is None:
                    session.add(
                        PluginBackgroundTurnJobModel(
                            source_event_id=existing.id,
                            plugin_id=plugin_id,
                            target_type=target.target_type,
                            target_id=target.target_id,
                            bot_user_id=grant.bot_user_id,
                            agent_intent=request.agent_intent,
                            status="pending",
                            attempts=0,
                            max_attempts=3,
                            next_attempt_at=now,
                            lease_until=None,
                            generated_text="",
                            tool_calls_used=0,
                            model_requests=0,
                            last_error_category=None,
                            created_at=now,
                            updated_at=now,
                            completed_at=None,
                        )
                    )
                    job_created = True
            return NotificationPublishReceipt(
                notification_id=notification_id,
                source_event_id=existing.id,
                event_created=event_created,
                delivery_enqueued=delivery_enqueued,
                agent_turn_enqueued=job_created,
                deduplicated=not event_created,
            )

    async def _resolve_publication_scope(
        self,
        *,
        plugin_id: str,
        target: NotificationTarget,
    ) -> tuple[ConversationScope, str]:
        """Resolve authorization separately from the target conversation identity."""

        async with self._database.sessions() as session:
            installation = await session.get(PluginInstallationModel, plugin_id)
            if installation is None or not installation.enabled or installation.status != "running":
                raise PluginPermissionError("plugin is not running")
            grant = await session.scalar(
                select(PluginBackgroundTargetGrantModel).where(
                    PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                    PluginBackgroundTargetGrantModel.target_type == target.target_type,
                    PluginBackgroundTargetGrantModel.target_id == target.target_id,
                    PluginBackgroundTargetGrantModel.enabled.is_(True),
                )
            )
            if grant is None:
                raise PluginPermissionError("notification target is not granted")
            if target.target_type == "group":
                group = await session.get(GroupModel, target.target_id)
                if group is None or not group.enabled:
                    raise PluginPermissionError("notification group is unknown or disabled")
                scope = ConversationScope.group(grant.bot_user_id, target.target_id)
            else:
                if await session.get(PersonModel, target.target_id) is None:
                    raise PluginPermissionError("notification private target is unknown")
                scope = ConversationScope.private(grant.bot_user_id, target.target_id)
        return scope, grant.bot_user_id

    async def claim_outbox(self, *, lease_seconds: int = 60) -> OutboxRecord | None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.scalar(
                select(PluginNotificationOutboxModel)
                .where(
                    PluginNotificationOutboxModel.next_attempt_at <= now,
                    or_(
                        PluginNotificationOutboxModel.status == "pending",
                        (
                            (PluginNotificationOutboxModel.status == "processing")
                            & (PluginNotificationOutboxModel.lease_until < now)
                        ),
                    ),
                )
                .order_by(
                    PluginNotificationOutboxModel.created_at, PluginNotificationOutboxModel.id
                )
                .limit(1)
            )
            if row is None:
                return None
            row.status = "processing"
            row.attempts += 1
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            await session.flush()
            return _outbox_record(row)

    async def finish_outbox(
        self,
        item_id: int,
        *,
        status: str,
        platform_message_id: str | None = None,
        error_category: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PluginNotificationOutboxModel, item_id)
            if row is None:
                return
            row.status = status
            row.platform_message_id = platform_message_id
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now
            row.sent_at = now if status == "sent" else None

    async def retry_outbox(self, item_id: int, *, error_category: str) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PluginNotificationOutboxModel, item_id)
            if row is None:
                return
            if row.attempts >= row.max_attempts:
                row.status = "failed"
            else:
                delays = (10, 30, 120, 600, 1800)
                row.status = "pending"
                row.next_attempt_at = now + timedelta(seconds=delays[min(row.attempts - 1, 4)])
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now

    async def claim_turn(self, *, lease_seconds: int = 120) -> BackgroundTurnJobRecord | None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.scalar(
                select(PluginBackgroundTurnJobModel)
                .where(
                    PluginBackgroundTurnJobModel.next_attempt_at <= now,
                    or_(
                        PluginBackgroundTurnJobModel.status == "pending",
                        (
                            (PluginBackgroundTurnJobModel.status == "processing")
                            & (PluginBackgroundTurnJobModel.lease_until < now)
                        ),
                    ),
                )
                .order_by(PluginBackgroundTurnJobModel.created_at, PluginBackgroundTurnJobModel.id)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "processing"
            row.attempts += 1
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            await session.flush()
            return _turn_record(row)

    async def finish_turn(
        self,
        job_id: int,
        *,
        text: str,
        tool_calls_used: int,
        model_requests: int,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            job = await session.get(PluginBackgroundTurnJobModel, job_id)
            if job is None:
                return
            job.status = "completed"
            job.generated_text = text[:24_000]
            job.tool_calls_used = tool_calls_used
            job.model_requests = model_requests
            job.lease_until = None
            job.updated_at = now
            job.completed_at = now
            if text.strip():
                existing = await session.scalar(
                    select(PluginNotificationOutboxModel).where(
                        PluginNotificationOutboxModel.source_event_id == job.source_event_id,
                        PluginNotificationOutboxModel.part_key == "agent_reply",
                    )
                )
                if existing is None:
                    notification_id = await session.scalar(
                        select(PluginNotificationOutboxModel.notification_id)
                        .where(PluginNotificationOutboxModel.source_event_id == job.source_event_id)
                        .limit(1)
                    ) or _notification_id(
                        job.plugin_id,
                        f"source:{job.source_event_id}",
                        job.target_type,
                        job.target_id,
                    )
                    session.add(
                        PluginNotificationOutboxModel(
                            notification_id=notification_id,
                            part_key="agent_reply",
                            source_event_id=job.source_event_id,
                            plugin_id=job.plugin_id,
                            target_type=job.target_type,
                            target_id=job.target_id,
                            bot_user_id=job.bot_user_id,
                            part_type="agent_reply",
                            text=text[:12_000],
                            media_handle_id=None,
                            status="pending",
                            attempts=0,
                            max_attempts=5,
                            next_attempt_at=now,
                            lease_until=None,
                            platform_message_id=None,
                            last_error_category=None,
                            created_at=now,
                            updated_at=now,
                            sent_at=None,
                        )
                    )

    async def fail_turn(self, job_id: int, *, error_category: str) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PluginBackgroundTurnJobModel, job_id)
            if row is None:
                return
            if row.attempts >= row.max_attempts:
                row.status = "failed"
            else:
                row.status = "pending"
                row.next_attempt_at = now + timedelta(seconds=(30, 120, 600)[row.attempts - 1])
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now

    async def abandon_turn(self, job_id: int, *, error_category: str) -> None:
        """Permanently stop a background turn that is unsafe to repeat."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PluginBackgroundTurnJobModel, job_id)
            if row is None:
                return
            row.status = "failed"
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now

    async def defer_turn(
        self,
        job_id: int,
        *,
        error_category: str,
        delay_seconds: int = 5,
        preserve_attempt: bool = False,
    ) -> None:
        """Return an unstarted/interrupted turn to the queue without losing its lease."""

        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(PluginBackgroundTurnJobModel, job_id)
            if row is None:
                return
            row.status = "pending"
            if preserve_attempt:
                row.attempts = max(0, row.attempts - 1)
            row.next_attempt_at = now + timedelta(seconds=max(1, delay_seconds))
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now

    async def counts(self, plugin_id: str) -> dict[str, int]:
        async with self._database.sessions() as session:
            outbox = (
                (
                    await session.execute(
                        select(PluginNotificationOutboxModel.status).where(
                            PluginNotificationOutboxModel.plugin_id == plugin_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            turns = (
                (
                    await session.execute(
                        select(PluginBackgroundTurnJobModel.status).where(
                            PluginBackgroundTurnJobModel.plugin_id == plugin_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        result: dict[str, int] = {}
        for prefix, values in (("outbox", outbox), ("turn", turns)):
            for value in values:
                key = f"{prefix}_{value}"
                result[key] = result.get(key, 0) + 1
        return result


async def _ensure_outbox_part(
    session: AsyncSession,
    *,
    notification_id: str,
    part_key: str,
    source_event_id: int,
    plugin_id: str,
    grant: PluginBackgroundTargetGrantModel,
    part_type: str,
    text: str,
    media_handle_id: str | None,
    now: datetime,
) -> bool:
    existing = await session.scalar(
        select(PluginNotificationOutboxModel).where(
            PluginNotificationOutboxModel.notification_id == notification_id,
            PluginNotificationOutboxModel.part_key == part_key,
        )
    )
    if existing is not None:
        return False
    session.add(
        PluginNotificationOutboxModel(
            notification_id=notification_id,
            part_key=part_key,
            source_event_id=source_event_id,
            plugin_id=plugin_id,
            target_type=grant.target_type,
            target_id=grant.target_id,
            bot_user_id=grant.bot_user_id,
            part_type=part_type,
            text=text,
            media_handle_id=media_handle_id,
            status="pending",
            attempts=0,
            max_attempts=5,
            next_attempt_at=now,
            lease_until=None,
            platform_message_id=None,
            last_error_category=None,
            created_at=now,
            updated_at=now,
            sent_at=None,
        )
    )
    return True


def _grant_view(row: PluginBackgroundTargetGrantModel) -> BackgroundTargetGrantView:
    return BackgroundTargetGrantView(
        target_type=row.target_type,
        target_id=row.target_id,
        bot_user_id=row.bot_user_id,
        enabled=row.enabled,
        created_by_user_id=row.created_by_user_id,
    )


def _outbox_record(row: PluginNotificationOutboxModel) -> OutboxRecord:
    return OutboxRecord(
        id=row.id,
        notification_id=row.notification_id,
        source_event_id=row.source_event_id,
        plugin_id=row.plugin_id,
        target_type=row.target_type,
        target_id=row.target_id,
        bot_user_id=row.bot_user_id,
        part_type=row.part_type,
        text=row.text,
        media_handle_id=row.media_handle_id,
        attempts=row.attempts,
    )


def _turn_record(row: PluginBackgroundTurnJobModel) -> BackgroundTurnJobRecord:
    return BackgroundTurnJobRecord(
        id=row.id,
        source_event_id=row.source_event_id,
        plugin_id=row.plugin_id,
        target_type=row.target_type,
        target_id=row.target_id,
        bot_user_id=row.bot_user_id,
        agent_intent=row.agent_intent,
        attempts=row.attempts,
    )


def _notification_id(plugin_id: str, event_key: str, target_type: str, target_id: str) -> str:
    raw = f"{plugin_id}\0{event_key}\0{target_type}\0{target_id}".encode()
    return hashlib.sha256(raw).hexdigest()


def _external_platform_id(plugin_id: str, event_key: str, target: NotificationTarget) -> str:
    digest = _notification_id(plugin_id, event_key, target.target_type, target.target_id)
    return f"external-{digest}"[:128]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
