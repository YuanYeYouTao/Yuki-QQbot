"""Host guarantees for plugin external events, Outbox, grants, and artifacts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.people_repository import GroupSettingsRepository, PeopleRepository
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginMediaArtifactModel,
    PluginNotificationOutboxModel,
)
from qq_ai_bot.plugin_host.media_artifacts import PluginMediaArtifactStore
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

PLUGIN_ID = "test-notifications"


async def _running_plugin(database: Database) -> None:
    repository = PluginInstallationRepository(database)
    await repository.upsert_discovered(
        plugin_id=PLUGIN_ID,
        name="Test",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("notification.publish", "notification.agent"),
    )
    await repository.approve(PLUGIN_ID)
    await repository.set_enabled(PLUGIN_ID, enabled=True)
    await repository.set_status(PLUGIN_ID, status="running")


@pytest.mark.asyncio
async def test_publish_is_atomic_idempotent_and_external(database: Database) -> None:
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    request = PublishNotificationRequest(
        event_key="github:owner/repo:PushEvent:1",
        event_type="PushEvent",
        external_source="github",
        target=target,
        occurred_at=datetime.now(UTC),
        summary="owner/repo 推送了一个提交",
        payload={"repository": "owner/repo"},
        text="通知正文",
        ask_agent=True,
        agent_intent="自然回应",
    )

    first = await notifications.publish(plugin_id=PLUGIN_ID, request=request)
    second = await notifications.publish(plugin_id=PLUGIN_ID, request=request)

    assert first.event_created and first.agent_turn_enqueued
    assert second.deduplicated and not second.event_created
    async with database.sessions() as session:
        events = int(await session.scalar(select(func.count(ChatEventModel.id))) or 0)
        outbox = int(
            await session.scalar(select(func.count(PluginNotificationOutboxModel.id))) or 0
        )
        turns = int(await session.scalar(select(func.count(PluginBackgroundTurnJobModel.id))) or 0)
        event = await session.get(ChatEventModel, first.source_event_id)
    assert (events, outbox, turns) == (1, 1, 1)
    assert event is not None
    assert event.direction == "external"
    assert event.origin == "plugin_background"
    assert event.sender_user_id == "9999"


@pytest.mark.asyncio
async def test_revoked_target_rejects_new_publication(database: Database) -> None:
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    assert await notifications.revoke_target(plugin_id=PLUGIN_ID, target=target)
    with pytest.raises(PluginPermissionError, match="not granted"):
        await notifications.publish(
            plugin_id=PLUGIN_ID,
            request=PublishNotificationRequest(
                event_key="event-2",
                event_type="test",
                external_source="test",
                target=target,
                occurred_at=datetime.now(UTC),
                summary="test",
            ),
        )


@pytest.mark.asyncio
async def test_media_artifact_is_opaque_bounded_and_plugin_scoped(
    database: Database,
    tmp_path: Path,
) -> None:
    await _running_plugin(database)
    store = PluginMediaArtifactStore(database, root=tmp_path / "artifacts")
    png = b"\x89PNG\r\n\x1a\n" + b"bounded-test"
    handle = await store.create(
        plugin_id=PLUGIN_ID,
        data=png,
        content_type="image/png",
        filename="card.png",
        ttl_seconds=60,
        storage_mb=1,
    )
    resolved = await store.resolve(plugin_id=PLUGIN_ID, handle_id=handle.handle_id)
    assert await asyncio.to_thread(resolved.local_path.read_bytes) == png
    assert str(resolved.local_path) not in handle.model_dump_json()
    with pytest.raises(PluginPermissionError, match="foreign"):
        await store.resolve(plugin_id="other-plugin", handle_id=handle.handle_id)


@pytest.mark.asyncio
async def test_completed_media_can_expire_without_breaking_publish_idempotency(
    database: Database,
    tmp_path: Path,
) -> None:
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    store = PluginMediaArtifactStore(database, root=tmp_path / "artifacts")
    handle = await store.create(
        plugin_id=PLUGIN_ID,
        data=b"\x89PNG\r\n\x1a\nsmall",
        content_type="image/png",
        filename="card.png",
        ttl_seconds=60,
        storage_mb=1,
    )
    request = PublishNotificationRequest(
        event_key="media-event",
        event_type="PushEvent",
        external_source="github",
        target=target,
        occurred_at=datetime.now(UTC),
        summary="push",
        media_handles=(handle.handle_id,),
    )
    await notifications.publish(plugin_id=PLUGIN_ID, request=request)
    item = await notifications.claim_outbox()
    assert item is not None
    await notifications.finish_outbox(item.id, status="sent", platform_message_id="123")
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(PluginMediaArtifactModel)
            .where(PluginMediaArtifactModel.handle_id == handle.handle_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await store.cleanup() == 1

    receipt = await notifications.publish(plugin_id=PLUGIN_ID, request=request)

    assert receipt.deduplicated
    assert not receipt.delivery_enqueued
