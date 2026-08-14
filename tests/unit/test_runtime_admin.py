"""Runtime configuration, audit, target binding, and administrator tool tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.admin.action_service import AdminActionService, TargetResolver
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.capabilities import AdminCapabilityService
from qq_ai_bot.admin.config_registry import ConfigRegistry
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor, ConfigApplyMode
from qq_ai_bot.automation.models import AutomationScript, AutomationStatus
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.container import ApplicationContainer
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatRequest,
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import LLMEmptyResponseError, LLMUnavailableError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.enums import MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryFactModel
from qq_ai_bot.persistence.repositories import (
    GroupSettingsRepository,
    RelationshipRepository,
    UserProfileRepository,
)
from qq_ai_bot.services.admin.group_admin import GroupAdminService
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.private_access_admin import PrivateAccessAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.user_profiles import UserProfileService
from qq_ai_bot.time.service import TimeContextService


def actor(
    user_id: str = "9000",
    *,
    message_id: str = "admin-1",
    text: str = "",
    group_id: str | None = None,
    mentions: tuple[str, ...] = (),
) -> AdminActor:
    return AdminActor(
        user_id=user_id,
        is_superuser=user_id == "9000",
        trigger_message_id=message_id,
        conversation_key=f"group:{group_id}" if group_id else f"private:{user_id}",
        current_group_id=group_id,
        mentioned_user_ids=mentions,
        current_message_text=text,
    )


def inbound(
    text: str,
    *,
    user_id: str = "9000",
    message_id: str = "admin-message",
    group_id: str | None = None,
    mentions: tuple[str, ...] = (),
    reply_text: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname="管理员"),
        text=text,
        raw_text=text,
        bot_user_id="7777",
        group_id=group_id,
        mentions_bot=bool(group_id),
        mentioned_user_ids=mentions,
        reply_text=reply_text,
    )


def admin_stack(
    database: Database,
) -> tuple[RuntimeConfigService, AdminCapabilityService]:
    """Build the shared administrator capability backend used by chat and commands."""

    settings = make_settings(database.url)
    harness = build_harness(database, settings)
    runtime = RuntimeConfigService(settings=settings, database=database)
    audit = AdminAuditService(database)
    relationship_admin = RelationshipAdminService(
        settings=settings,
        relationships=harness.relationships,
        audit=audit,
        runtime_config=runtime,
    )
    memory_admin = MemoryAdminService(
        settings=settings,
        memories=harness.processor._memories,
        audit=audit,
    )
    preference_admin = PreferenceAdminService(
        settings=settings,
        memories=harness.processor._memories,
        audit=audit,
    )
    group_admin = GroupAdminService(
        settings=settings,
        groups=harness.groups,
        runtime_config=runtime,
        audit=audit,
    )
    private_admin = PrivateAccessAdminService(
        settings=settings,
        private_users=harness.private_users,
        audit=audit,
        runtime_config=runtime,
    )
    actions = AdminActionService(
        settings=settings,
        relationships=relationship_admin,
        memories=memory_admin,
        preferences=preference_admin,
        groups=group_admin,
        private_access=private_admin,
    )
    capabilities = AdminCapabilityService(
        settings=settings,
        runtime_config=runtime,
        actions=actions,
        audit=audit,
    )
    return runtime, capabilities


def test_registry_is_explicit_and_converts_supported_types() -> None:
    registry = ConfigRegistry()
    assert "planner.max_pending_messages" in registry.keys
    assert "llm_api_key" not in registry.keys
    assert "system_prompt" not in registry.keys
    assert registry.convert(registry.get("planner.max_pending_messages"), "10") == 10
    assert registry.convert(registry.get("llm.temperature"), "0.25") == 0.25
    assert registry.convert(registry.get("planner.group_enabled"), "开启") is True
    assert registry.get("desired_messages").key == "planner.preferred_messages"
    assert registry.convert(registry.get("日常回复条数"), "5") == 5
    emoji_frequency = registry.get("日常表情频率")
    assert emoji_frequency.key == "emoji.spontaneous_frequency"
    assert registry.convert(emoji_frequency, "0.1") == 0.1
    with pytest.raises(KeyError):
        registry.get("arbitrary_config_set")
    with pytest.raises(ValueError):
        registry.convert(registry.get("planner.max_pending_messages"), 10000)


def test_registry_exposes_reviewed_vision_configuration_only() -> None:
    registry = ConfigRegistry()
    vision = registry.list("vision")
    by_mode = {
        mode: {spec.key for spec in vision if spec.apply_mode is mode} for mode in ConfigApplyMode
    }

    assert by_mode[ConfigApplyMode.HOT] == {
        "vision.max_images_per_turn",
        "vision.max_frames_per_turn",
        "vision.gif_max_frames",
        "vision.thinking_enabled",
        "vision.thinking_budget",
        "vision.low_confidence_retry_threshold",
        "vision.per_user_requests_per_minute",
        "vision.per_group_requests_per_minute",
    }
    assert by_mode[ConfigApplyMode.FUTURE_ONLY] == {"vision.analysis_retention_days"}
    assert by_mode[ConfigApplyMode.RESTART_REQUIRED] == {
        "vision.enabled",
        "vision.base_url",
        "vision.model",
        "vision.global_concurrency",
        "vision.queue_max_pending",
        "vision.queue_timeout_seconds",
        "vision.media_download_timeout_seconds",
        "vision.timeout_seconds",
        "vision.max_output_tokens",
    }
    secret = registry.get("vision.api_key")
    assert secret.apply_mode is ConfigApplyMode.SECRET
    assert secret.sensitive
    assert "vision.max_download_bytes" not in registry.keys


@pytest.mark.asyncio
async def test_runtime_snapshot_resolves_dynamic_vision_settings(database: Database) -> None:
    settings = make_settings(database.url)
    service = RuntimeConfigService(settings=settings, database=database)
    for key, value in (
        ("vision.max_images_per_turn", 4),
        ("vision.max_frames_per_turn", 12),
        ("vision.gif_max_frames", 6),
        ("vision.thinking_enabled", False),
        ("vision.thinking_budget", 4096),
        ("vision.low_confidence_retry_threshold", 0.72),
        ("vision.per_user_requests_per_minute", 8),
        ("vision.per_group_requests_per_minute", 20),
        ("vision.analysis_retention_days", 14),
    ):
        result = await service.set_override(
            key,
            value,
            scope_type="global",
            scope_id="",
            actor_user_id="9000",
            trigger_message_id=f"vision-{key}",
        )
        assert result.success

    vision = (await service.snapshot(user_id="1001", group_id="2001")).vision
    assert vision.max_images_per_turn == 4
    assert vision.max_frames_per_turn == 12
    assert vision.gif_max_frames == 6
    assert not vision.thinking_enabled
    assert vision.thinking_budget == 4096
    assert vision.low_confidence_retry_threshold == 0.72
    assert vision.per_user_requests_per_minute == 8
    assert vision.per_group_requests_per_minute == 20
    assert vision.analysis_retention_days == 14


@pytest.mark.asyncio
async def test_runtime_precedence_delete_and_audit(database: Database) -> None:
    settings = make_settings(database.url, local_context_event_limit=30)
    service = RuntimeConfigService(settings=settings, database=database)
    global_change = await service.set_override(
        "context.local_event_limit",
        40,
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="global",
    )
    group_change = await service.set_override(
        "context.local_event_limit",
        50,
        scope_type="group",
        scope_id="2001",
        actor_user_id="9000",
        trigger_message_id="group",
    )
    user_change = await service.set_override(
        "context.local_event_limit",
        60,
        scope_type="user",
        scope_id="1001",
        actor_user_id="9000",
        trigger_message_id="user",
    )
    assert global_change.version == 1
    assert group_change.version == 1
    assert user_change.version == 1
    assert (await service.get_effective("context.local_event_limit")).value == 40
    assert (await service.get_effective("context.local_event_limit", group_id="2001")).value == 50
    assert (
        await service.get_effective(
            "context.local_event_limit",
            user_id="1001",
            group_id="2001",
        )
    ).value == 60
    deleted = await service.delete_override(
        "context.local_event_limit",
        scope_type="user",
        scope_id="1001",
        actor_user_id="9000",
        trigger_message_id="delete",
    )
    assert deleted.success
    assert (
        await service.get_effective(
            "context.local_event_limit",
            user_id="1001",
            group_id="2001",
        )
    ).value == 50
    history = await service.history(actor_user_id="9000")
    assert len(history) == 4
    assert history[0].before != history[0].after


@pytest.mark.asyncio
async def test_business_mutation_and_admin_audit_share_one_transaction(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(database.url)
    groups = GroupSettingsRepository(database)
    audit = AdminAuditService(database)
    service = GroupAdminService(
        settings=settings,
        groups=groups,
        runtime_config=RuntimeConfigService(settings=settings, database=database),
        audit=audit,
    )

    enabled = await service.enable_current_group(actor(), "2001")
    history = await audit.history(capability="group")
    assert enabled.enabled
    assert len(history) == 1
    assert history[0].success
    assert history[0].target_id == "2001"

    async def fail_audit_insert(**_kwargs: object) -> object:
        raise RuntimeError("simulated audit insert failure")

    monkeypatch.setattr(audit, "record", fail_audit_insert)
    with pytest.raises(RuntimeError, match="simulated audit insert failure"):
        await service.enable_current_group(actor(message_id="rollback"), "2002")

    assert await groups.get("2002") is None


@pytest.mark.asyncio
async def test_secret_immutable_unknown_and_cross_key_validation(database: Database) -> None:
    settings = make_settings(
        database.url,
        llm_api_key="top-secret",
        vision_api_key="vision-top-secret",
    )
    service = RuntimeConfigService(settings=settings, database=database)
    secret = await service.get_effective("llm.api_key")
    assert secret.value is None
    assert secret.configured is True
    vision_secret = await service.get_effective("vision.api_key")
    assert vision_secret.value is None
    assert vision_secret.configured is True
    rejected_secret_change = await service.set_override(
        "vision.api_key",
        "replacement-vision-secret",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="vision-secret",
    )
    assert not rejected_secret_change.success
    assert rejected_secret_change.error_category == "permission_denied"
    immutable = await service.set_override(
        "superusers",
        "12345",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="immutable",
    )
    assert not immutable.success
    assert immutable.error_category == "permission_denied"
    unknown = await service.set_override(
        "unknown.key",
        "secret-attempt",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="unknown",
    )
    assert not unknown.success
    assert unknown.error_category == "unknown_key"
    invalid_delay = await service.set_override(
        "reply.delay_min_seconds",
        10,
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="delay",
    )
    assert not invalid_delay.success
    assert "不能大于" in invalid_delay.detail
    all_audit = await AdminAuditService(database).history(limit=10)
    rendered = json.dumps([row.after for row in all_audit], ensure_ascii=False)
    assert "top-secret" not in rendered
    assert "vision-top-secret" not in rendered
    assert "replacement-vision-secret" not in rendered
    assert "secret-attempt" not in rendered


@pytest.mark.asyncio
async def test_restart_required_pending_then_activates_on_new_service(
    database: Database,
) -> None:
    settings = make_settings(database.url, llm_model="old-model")
    current = RuntimeConfigService(settings=settings, database=database)
    await current.initialize()
    change = await current.set_override(
        "llm.model",
        "new-model",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="restart",
    )
    assert change.success
    assert change.pending_restart
    assert change.apply_mode is ConfigApplyMode.RESTART_REQUIRED
    assert (await current.get_effective("llm.model")).value == "old-model"
    assert await current.pending_restart_count() == 1

    restarted = RuntimeConfigService(settings=settings, database=database)
    await restarted.initialize()
    assert (await restarted.get_effective("llm.model")).value == "new-model"
    assert await restarted.pending_restart_count() == 0
    assert (await restarted.startup_settings_updates())["llm_model"] == "new-model"


@pytest.mark.asyncio
async def test_vision_restart_overrides_map_to_startup_settings(database: Database) -> None:
    settings = make_settings(database.url, vision_enabled=False)
    current = RuntimeConfigService(settings=settings, database=database)
    await current.initialize()
    for key, value in (
        ("vision.enabled", True),
        ("vision.base_url", "https://dashscope.example/v1"),
        ("vision.model", "new-vision-model"),
        ("vision.global_concurrency", 3),
        ("vision.queue_max_pending", 48),
        ("vision.queue_timeout_seconds", 90),
        ("vision.media_download_timeout_seconds", 75),
        ("vision.timeout_seconds", 45),
        ("vision.max_output_tokens", 16384),
    ):
        change = await current.set_override(
            key,
            value,
            scope_type="global",
            scope_id="",
            actor_user_id="9000",
            trigger_message_id=f"restart-{key}",
        )
        assert change.success
        assert change.pending_restart

    restarted = RuntimeConfigService(settings=settings, database=database)
    await restarted.initialize()
    assert await restarted.startup_settings_updates() == {
        "llm_model": settings.llm_model,
        "llm_timeout_seconds": settings.llm_timeout_seconds,
        "llm_max_retries": settings.llm_max_retries,
        "global_llm_concurrency": settings.global_llm_concurrency,
        "web_global_concurrency": settings.web_global_concurrency,
        "per_user_requests_per_minute": settings.per_user_requests_per_minute,
        "per_group_requests_per_minute": settings.per_group_requests_per_minute,
        "vision_enabled": True,
        "vision_base_url": "https://dashscope.example/v1",
        "vision_model": "new-vision-model",
        "vision_global_concurrency": 3,
        "vision_queue_max_pending": 48,
        "vision_queue_timeout_seconds": 90.0,
        "vision_media_download_timeout_seconds": 75.0,
        "vision_timeout_seconds": 45.0,
        "vision_max_output_tokens": 16384,
        "speech_enabled": settings.speech_enabled,
        "speech_provider": settings.speech_provider,
        "speech_socket_path": settings.speech_socket_path,
        "speech_root": settings.speech_root,
        "genie_data_dir": settings.genie_data_dir,
        "automation_enabled": settings.automation_enabled,
        "automation_poll_seconds": settings.automation_poll_seconds,
        "automation_lease_seconds": settings.automation_lease_seconds,
        "automation_max_active_per_superuser": settings.automation_max_active_per_superuser,
        "automation_max_active_per_user": settings.automation_max_active_per_user,
        "automation_max_steps": settings.automation_max_steps,
        "automation_max_llm_calls_per_run": settings.automation_max_llm_calls_per_run,
        "automation_max_tool_calls_per_run": settings.automation_max_tool_calls_per_run,
        "automation_max_messages_per_run": settings.automation_max_messages_per_run,
        "automation_max_runtime_seconds": settings.automation_max_runtime_seconds,
        "automation_min_interval_seconds": settings.automation_min_interval_seconds,
        "automation_default_misfire_grace_seconds": (
            settings.automation_default_misfire_grace_seconds
        ),
        "automation_max_consecutive_failures": settings.automation_max_consecutive_failures,
        "automation_run_retention_days": settings.automation_run_retention_days,
    }


@pytest.mark.asyncio
async def test_container_factory_activates_restart_overrides_before_build(
    database: Database,
) -> None:
    settings = make_settings(database.url, llm_model="old-model")
    service = RuntimeConfigService(settings=settings, database=database)
    result = await service.set_override(
        "llm.model",
        "active-after-restart",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="container-restart",
    )
    assert result.success
    container = await ApplicationContainer.create(settings)
    try:
        assert container.settings.llm_model == "active-after-restart"
        assert (await container.runtime_config.get_effective("llm.model")).value == (
            "active-after-restart"
        )
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_future_only_initial_scores_affect_only_new_relationships(
    database: Database,
) -> None:
    settings = make_settings(database.url)
    runtime = RuntimeConfigService(settings=settings, database=database)
    relationships = RelationshipRepository(database)
    existing = await relationships.get_or_create("1001")
    assert (existing.affection_score, existing.trust_score) == (50, 50)
    for key, value in (
        ("relationship.initial_affection", 80),
        ("relationship.initial_trust", 70),
    ):
        change = await runtime.set_override(
            key,
            value,
            scope_type="global",
            scope_id="",
            actor_user_id="9000",
            trigger_message_id=key,
        )
        assert change.success
        assert change.apply_mode is ConfigApplyMode.FUTURE_ONLY

    profiles = UserProfileService(
        UserProfileRepository(database),
        runtime,
    )
    await profiles.capture(inbound("你好", user_id="1002", message_id="new-person"))
    unchanged = await relationships.get("1001")
    created = await relationships.get("1002")
    assert unchanged is not None and created is not None
    assert (unchanged.affection_score, unchanged.trust_score) == (50, 50)
    assert (created.affection_score, created.trust_score) == (80, 70)


@pytest.mark.asyncio
async def test_rollback_is_actor_bound_and_detects_newer_changes(database: Database) -> None:
    settings = make_settings(database.url)
    service = RuntimeConfigService(settings=settings, database=database)
    first = await service.set_override(
        "planner.max_pending_messages",
        8,
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="first",
    )
    assert first.change_id is not None
    denied = await service.rollback(first.change_id, actor_user_id="9001")
    assert not denied.success
    assert denied.error_category == "permission_denied"

    second = await service.set_override(
        "planner.max_pending_messages",
        9,
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="second",
    )
    assert second.version == 2
    conflicted = await service.rollback(first.change_id, actor_user_id="9000")
    assert not conflicted.success
    assert conflicted.error_category == "rollback_conflict"
    assert second.change_id is not None
    restored = await service.rollback(second.change_id, actor_user_id="9000")
    assert restored.success
    assert restored.after == 8
    assert restored.version == 3


@pytest.mark.asyncio
async def test_concurrent_same_key_updates_increment_version(database: Database) -> None:
    service = RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    )
    changes = await asyncio.gather(
        *(
            service.set_override(
                "planner.max_pending_messages",
                value,
                scope_type="global",
                scope_id="",
                actor_user_id="9000",
                trigger_message_id=f"concurrent-{value}",
            )
            for value in (4, 5, 6, 7)
        )
    )
    assert sorted(change.version for change in changes) == [1, 2, 3, 4]


def test_target_resolver_rejects_fabricated_ids_and_accepts_real_mentions() -> None:
    current = actor(
        text="把 @张三 的好感度降低 5",
        group_id="2001",
        mentions=("12345678",),
    )
    assert (
        TargetResolver.user(
            {"target": "mentioned_user", "user_id": "12345678"},
            current,
        )
        == "12345678"
    )
    with pytest.raises(ValueError):
        TargetResolver.user(
            {"target": "explicit_user_id", "user_id": "87654321"},
            current,
        )
    assert TargetResolver.group({"target": "current_group"}, current) == "2001"


@pytest.mark.asyncio
async def test_single_chat_agent_executes_relationship_admin_tool(
    database: Database,
) -> None:
    calls = 0

    def responder(_request: object) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="relationship-1",
                        function=ToolFunction(
                            name="admin_execute_action",
                            arguments=json.dumps(
                                {
                                    "action": "relationship.set_affection",
                                    "arguments": {"target": "self", "value": 100},
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="已按真实结果调整。", latency_seconds=0)

    settings = make_settings(database.url)
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    _, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("把我的好感度调到一百"),
        sender,
    )
    assert result.reason == "chat"
    assert sender.messages[0].text == "已按真实结果调整。"
    assert (await harness.relationships.get("9000")).affection_score == 100  # type: ignore[union-attr]
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_natural_language_config_change_is_hot_and_group_scoped(
    database: Database,
) -> None:
    calls = 0

    def responder(_request: object) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="group-config",
                        function=ToolFunction(
                            name="admin_set_config",
                            arguments=json.dumps(
                                {
                                    "key": "planner.max_pending_messages",
                                    "value": 10,
                                    "scope_type": "group",
                                    "scope_id": "current_group",
                                }
                            ),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="本群上限已立即改为 10 次。", latency_seconds=0)

    settings = make_settings(database.url)
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    runtime, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound(
            "把本群每小时自动插话次数改成 10",
            group_id="2001",
            message_id="natural-config",
        ),
        sender,
    )
    assert result.reason == "chat"
    assert sender.messages[0].text == "本群上限已立即改为 10 次。"
    assert (await runtime.snapshot(group_id="2001")).planner.max_pending_messages == 10
    assert (await runtime.snapshot(group_id="2002")).planner.max_pending_messages == 8


@pytest.mark.asyncio
async def test_natural_language_can_change_planner_message_target(
    database: Database,
) -> None:
    calls = 0

    def responder(_request: object) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="planner-message-target",
                        function=ToolFunction(
                            name="admin_set_config",
                            arguments=json.dumps(
                                {
                                    "key": "desired_messages",
                                    "value": 5,
                                    "scope_type": "global",
                                    "scope_id": "",
                                }
                            ),
                        ),
                    ),
                ),
            )
        return ChatResponse(content="日常回复现在会尽量分成 5 条。", latency_seconds=0)

    settings = make_settings(database.url)
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    runtime, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)
    sender = MemorySender()

    result = await harness.processor.handle(
        inbound("把 Planner 日常回复偏好改成 5 条", message_id="planner-message-config"),
        sender,
    )

    assert result.reason == "chat"
    assert sender.messages[0].text == "日常回复现在会尽量分成 5 条。"
    assert (await runtime.snapshot()).planner.preferred_messages == 5


@pytest.mark.asyncio
async def test_same_management_text_from_normal_user_is_ordinary_chat(
    database: Database,
) -> None:
    def responder(request: ChatRequest) -> ChatResponse:
        assert not any(tool.name.startswith("admin_") for tool in request.tools)
        return ChatResponse(content="普通用户不能修改这个设置。", latency_seconds=0)

    settings = make_settings(database.url)
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    runtime, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound(
            "把每小时自动插话次数改成 10",
            user_id="1001",
            message_id="normal-user-management-text",
        ),
        sender,
    )
    assert result.reason == "chat"
    assert sender.messages[0].text == "普通用户不能修改这个设置。"
    assert (await runtime.get_effective("planner.max_pending_messages")).value == 8


@pytest.mark.asyncio
async def test_single_chat_agent_keeps_persona_and_context_across_missing_target(
    database: Database,
) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        tool_names = {tool.name for tool in request.tools}
        assert any(
            message.role == "system" and "Yuki" in (message.content or "")
            for message in request.messages
        )
        if calls == 1:
            assert "admin_request_clarification" not in tool_names
            assert "admin_execute_action" in tool_names
            return ChatResponse(
                content="主人，把奶龙的 QQ 号告诉我一下吧～",
                latency_seconds=0,
            )
        if calls == 2:
            assert "admin_request_clarification" not in tool_names
            assert "admin_execute_action" in tool_names
            history = [
                message.content or ""
                for message in request.messages
                if message.role in {"user", "assistant"}
            ]
            assert any("奶龙的好感度改成88" in text for text in history)
            assert any("奶龙的 QQ 号" in text for text in history)
            assert any("1808058482" in text for text in history)
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="single-agent-set-affection",
                        function=ToolFunction(
                            name="admin_execute_action",
                            arguments=json.dumps(
                                {
                                    "action": "relationship.set_affection",
                                    "arguments": {
                                        "target": "explicit_user_id",
                                        "user_id": "1808058482",
                                        "value": 88,
                                    },
                                }
                            ),
                        ),
                    ),
                ),
            )
        assert "admin_execute_action" in tool_names
        result = json.loads(
            next(
                message.content or "{}"
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        assert result["ok"] is True
        return ChatResponse(content="好啦主人，已经改成 88 了～", latency_seconds=0)

    settings = make_settings(database.url, system_prompt="你是 Yuki，说话自然简短。")
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    _runtime, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)

    first_sender = MemorySender()
    first = inbound("帮我把奶龙的好感度改成88", message_id="single-agent-first")
    first_result = await harness.processor.handle(first, first_sender)
    assert first_result.reason == "chat"
    assert first_sender.messages[0].text == "主人，把奶龙的 QQ 号告诉我一下吧～"

    second_sender = MemorySender()
    second = inbound("1808058482", message_id="single-agent-followup")
    second_result = await harness.processor.handle(second, second_sender)
    assert second_result.reason == "chat"
    assert second_sender.messages[0].text == "好啦主人，已经改成 88 了～"
    relationship = await harness.relationships.get("1808058482")
    assert relationship is not None
    assert relationship.affection_score == 88
    assert calls == 3


@pytest.mark.asyncio
async def test_single_chat_agent_tolerates_repeated_capability_lookup_then_sets_config(
    database: Database,
) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        tool_names = {tool.name for tool in request.tools}
        if calls == 1:
            assert "get_my_capabilities" in tool_names
            assert "admin_list_capabilities" not in tool_names
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="lookup-max-per-hour",
                        function=ToolFunction(
                            name="get_my_capabilities",
                            arguments=json.dumps(
                                {"mode": "focused", "query": "max pending messages"}
                            ),
                        ),
                    ),
                ),
            )
        if calls == 2:
            assert "get_my_capabilities" in tool_names
            assert "admin_list_capabilities" not in tool_names
            assert "admin_set_config" in tool_names
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="accidental-repeat-lookup",
                        function=ToolFunction(
                            name="get_my_capabilities",
                            arguments=json.dumps(
                                {"mode": "focused", "query": "max pending messages"}
                            ),
                        ),
                    ),
                ),
            )
        if calls == 3:
            repeat_result = json.loads(
                next(
                    message.content or "{}"
                    for message in reversed(request.messages)
                    if message.role == "tool"
                )
            )
            assert repeat_result["ok"] is True
            assert [item["id"] for item in repeat_result["data"]["capabilities"]] == [
                "config:planner.max_pending_messages"
            ]
            assert "admin_set_config" in tool_names
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="set-max-per-hour",
                        function=ToolFunction(
                            name="admin_set_config",
                            arguments=json.dumps(
                                {
                                    "key": "planner.max_pending_messages",
                                    "value": 30,
                                    "scope_type": "global",
                                    "scope_id": "",
                                }
                            ),
                        ),
                    ),
                ),
            )
        assert "admin_set_config" in tool_names
        changed = json.loads(
            next(
                message.content or "{}"
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        assert changed["ok"] is True
        return ChatResponse(content="好啦主人，每小时上限已经改成 30 了～", latency_seconds=0)

    settings = make_settings(database.url, system_prompt="你是 Yuki，说话自然简短。")
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    runtime, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)

    sender = MemorySender()
    message = inbound("max pending messages 改成30", message_id="repeat-capability-config")
    result = await harness.processor.handle(message, sender)

    assert result.reason == "chat"
    assert sender.messages[0].text == "好啦主人，每小时上限已经改成 30 了～"
    assert (await runtime.get_effective("planner.max_pending_messages")).value == 30
    assert calls == 4


@pytest.mark.asyncio
async def test_single_chat_agent_can_list_then_delete_memory_in_one_turn(
    database: Database,
) -> None:
    memory_service = MemoryFactService(MemoryFactRepository(database))
    memory = await memory_service.add_explicit_person(
        "9000",
        "自动插话上限设置为每小时10条",
        memory_key="old-auto-limit",
        limit=100,
    )
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        tool_names = {tool.name for tool in request.tools}
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="list-before-delete",
                        function=ToolFunction(
                            name="admin_execute_action",
                            arguments=json.dumps(
                                {"action": "memory.list", "arguments": {"target": "self"}}
                            ),
                        ),
                    ),
                ),
            )
        if calls == 2:
            listed = json.loads(
                next(
                    message.content or "{}"
                    for message in reversed(request.messages)
                    if message.role == "tool"
                )
            )
            assert listed["ok"] is True
            assert "admin_execute_action" in tool_names
            assert any(item["id"] == memory.id for item in listed["data"]["result"]["memories"])
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="delete-after-list",
                        function=ToolFunction(
                            name="admin_execute_action",
                            arguments=json.dumps(
                                {
                                    "action": "memory.delete",
                                    "arguments": {
                                        "target": "self",
                                        "memory_id": memory.id,
                                    },
                                }
                            ),
                        ),
                    ),
                ),
            )
        assert "admin_execute_action" in tool_names
        deleted = json.loads(
            next(
                message.content or "{}"
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        assert deleted["ok"] is True
        return ChatResponse(content="已经删掉那条旧记忆啦。", latency_seconds=0)

    settings = make_settings(database.url)
    provider = FakeLLMProvider(responder)
    harness = build_harness(database, settings, provider)
    _runtime, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)

    sender = MemorySender()
    message = inbound(
        "删除‘自动插话上限设置为每小时10条’这条记忆",
        message_id="list-then-delete-memory",
    )
    result = await harness.processor.handle(message, sender)

    assert result.reason == "chat"
    assert sender.messages[0].text == "已经删掉那条旧记忆啦。"
    remaining = await memory_service.list_person("9000", limit=100)
    assert all(item.id != memory.id for item in remaining)
    assert calls == 3


@pytest.mark.asyncio
async def test_single_chat_agent_executes_multiple_distinct_mutations_in_order(
    database: Database,
) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="config-write",
                        function=ToolFunction(
                            name="admin_set_config",
                            arguments=json.dumps(
                                {
                                    "key": "planner.max_pending_messages",
                                    "value": 10,
                                    "scope_type": "global",
                                    "scope_id": "",
                                }
                            ),
                        ),
                    ),
                    ToolCall(
                        id="memory-write",
                        function=ToolFunction(
                            name="admin_execute_action",
                            arguments=json.dumps(
                                {
                                    "action": "memory.add",
                                    "arguments": {
                                        "target": "self",
                                        "content": "允许顺序执行不同修改",
                                    },
                                }
                            ),
                        ),
                    ),
                ),
            )
        results = [
            json.loads(message.content or "{}")
            for message in request.messages
            if message.role == "tool"
        ]
        assert len(results) == 2
        assert all(item["ok"] is True for item in results)
        return ChatResponse(content="两项修改都完成了。", latency_seconds=0)

    settings = make_settings(database.url)
    harness = build_harness(database, settings, FakeLLMProvider(responder))
    runtime, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)

    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("把上限改为10并记住允许顺序执行不同修改", message_id="two-writes"),
        sender,
    )

    assert result.reason == "chat"
    assert (await runtime.get_effective("planner.max_pending_messages")).value == 10
    memories = await MemoryFactService(MemoryFactRepository(database)).list_person(
        "9000", limit=100
    )
    assert any(row.content == "允许顺序执行不同修改" for row in memories)


@pytest.mark.asyncio
async def test_agent_retries_empty_provider_response_after_tool_result(
    database: Database,
) -> None:
    calls = 0

    def responder(_request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="read-config",
                        function=ToolFunction(
                            name="admin_get_config",
                            arguments=json.dumps(
                                {
                                    "keys": ["agent.max_tool_calls"],
                                    "scope_type": "global",
                                    "scope_id": "",
                                }
                            ),
                        ),
                    ),
                ),
            )
        if calls == 2:
            raise LLMEmptyResponseError("synthetic empty response")
        return ChatResponse(content="已经根据工具结果继续回答。", latency_seconds=0)

    settings = make_settings(database.url)
    harness = build_harness(database, settings, FakeLLMProvider(responder))
    _runtime, capabilities = admin_stack(database)
    harness.processor._chat.set_admin_tools(capabilities)

    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("看看工具上限", message_id="empty-after-tool"), sender
    )

    assert result.reason == "chat"
    assert sender.messages[0].text == "已经根据工具结果继续回答。"
    assert calls == 3


@pytest.mark.asyncio
async def test_failed_automation_creation_cannot_be_reported_as_success(
    database: Database,
) -> None:
    calls = 0

    def responder(request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="invalid-automation",
                        function=ToolFunction(
                            name="automation_create",
                            arguments=json.dumps({"script": {"version": 1}}),
                        ),
                    ),
                ),
            )
        failed = json.loads(
            next(
                message.content or "{}"
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        assert failed["ok"] is False
        return ChatResponse(content="已经创建成功啦～", latency_seconds=0)

    settings = make_settings(database.url, automation_enabled=True)
    harness = build_harness(database, settings, FakeLLMProvider(responder))
    automation = AutomationService(
        settings=settings,
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(database),
    )
    harness.processor._chat.set_automation_tools(AutomationToolService(automation))

    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("每天清理旧记忆", message_id="invalid-automation-create"), sender
    )

    assert result.reason == "chat"
    assert sender.messages[0].text.startswith("操作未完成：")
    assert "创建成功" not in sender.messages[0].text
    assert await automation.list_current("9000") == ()


@pytest.mark.asyncio
async def test_successful_automation_cancel_survives_final_model_failure(
    database: Database,
) -> None:
    settings = make_settings(database.url, automation_enabled=True)
    automation = AutomationService(
        settings=settings,
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(database),
    )
    message = inbound("取消我的自动化任务", message_id="cancel-after-commit")
    task = await automation.create(
        AutomationScript.model_validate(
            {
                "version": 1,
                "name": "测试取消回执",
                "timezone": "Asia/Shanghai",
                "schedule": {"type": "after", "seconds": 300},
                "context": {"scene": "none"},
                "steps": [
                    {
                        "id": "deliver",
                        "call": "onebot.send_private_message",
                        "arguments": {"user_id": "$creator_user_id", "text": "测试"},
                    }
                ],
                "limits": {
                    "max_steps": 1,
                    "max_llm_calls": 0,
                    "max_tool_calls": 1,
                    "max_messages": 1,
                    "timeout_seconds": 30,
                },
            }
        ),
        inbound=message,
        conversation_key="private:9000",
    )
    calls = 0

    def responder(_request: ChatRequest) -> ChatResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="cancel-automation",
                        function=ToolFunction(
                            name="automation_cancel",
                            arguments=json.dumps({"automation_id": task.id}),
                        ),
                    ),
                ),
            )
        raise LLMUnavailableError("synthetic finalization failure")

    harness = build_harness(database, settings, FakeLLMProvider(responder))
    harness.processor._chat.set_automation_tools(AutomationToolService(automation))
    sender = MemorySender()

    result = await harness.processor.handle(message, sender)

    assert result.reason == "chat"
    assert [item.text for item in sender.messages] == ["任务已取消。"]
    stored = await automation.require_owned(task.id, "9000")
    assert stored.status is AutomationStatus.CANCELLED
    assert calls == 2


@pytest.mark.asyncio
async def test_memory_prune_action_deletes_only_old_low_automatic_memories(
    database: Database,
) -> None:
    memories = MemoryFactService(MemoryFactRepository(database))
    old = await memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="9000",
            kind="fact",
            memory_key="old-low",
            category="fact",
            content="过时低重要度记忆",
            importance=2,
            source_type=MemorySourceType.AUTOMATIC,
        ),
        limit=100,
    )
    recent = await memories.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="9000",
            kind="fact",
            memory_key="recent-low",
            category="fact",
            content="近期低重要度记忆",
            importance=1,
            source_type=MemorySourceType.AUTOMATIC,
        ),
        limit=100,
    )
    explicit = await memories.add_explicit_person(
        "9000",
        "显式记忆永不由自动清理删除",
        memory_key="explicit-low",
        limit=100,
    )
    stale_at = datetime.now(UTC) - timedelta(days=8)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(MemoryFactModel)
            .where(MemoryFactModel.id.in_([old.id, explicit.id]))
            .values(updated_at=stale_at)
        )

    _runtime, capabilities = admin_stack(database)
    message = inbound("清理重要度1和2且超过7天的记忆", message_id="prune-memory")
    runtime = ToolRuntime(
        inbound=message,
        gateway=None,
        allow_generic_onebot=False,
        allow_admin_actions=True,
        conversation_key="private:9000",
        trigger_message_id=message.message_id,
        actor_user_id="9000",
        actor_is_superuser=True,
        current_group_id=None,
        mentioned_user_ids=(),
    )
    payload = json.loads(
        await capabilities.execute(
            "admin_execute_action",
            json.dumps(
                {
                    "action": "memory.prune",
                    "arguments": {
                        "target": "self",
                        "max_importance": 2,
                        "older_than_days": 7,
                    },
                }
            ),
            runtime,
        )
    )

    assert payload["ok"] is True
    assert payload["data"]["result"]["deleted_count"] == 1
    remaining_ids = {row.id for row in await memories.list_person("9000", limit=100)}
    assert old.id not in remaining_ids
    assert {recent.id, explicit.id} <= remaining_ids


@pytest.mark.asyncio
async def test_deterministic_config_command_uses_runtime_service(database: Database) -> None:
    harness = build_harness(database, make_settings(database.url))
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound(
            "/ai config set planner.max_pending_messages 9",
            message_id="deterministic-config",
        ),
        sender,
    )
    assert result.reason == "command_config"
    assert "已立即生效" in sender.messages[0].text
    assert (
        await harness.processor._runtime_config.get_effective("planner.max_pending_messages")
    ).value == 9


@pytest.mark.asyncio
async def test_group_override_is_visible_in_next_snapshot(database: Database) -> None:
    settings = make_settings(database.url)
    service = RuntimeConfigService(settings=settings, database=database)
    changed = await service.set_override(
        "planner.max_pending_messages",
        10,
        scope_type="group",
        scope_id="2001",
        actor_user_id="9000",
        trigger_message_id="group-runtime",
    )
    assert changed.success
    debounce = await service.set_override(
        "planner.group_debounce_seconds",
        1,
        scope_type="group",
        scope_id="2001",
        actor_user_id="9000",
        trigger_message_id="planner-debounce",
    )
    assert debounce.success
    assert (await service.snapshot(group_id="2001")).planner.max_pending_messages == 10
    assert (await service.snapshot(group_id="2001")).planner.group_debounce_seconds == 1
    assert (await service.snapshot(group_id="2002")).planner.max_pending_messages == 8
    assert (await service.snapshot(group_id="2002")).planner.group_debounce_seconds == 3


@pytest.mark.asyncio
async def test_user_override_wins_after_message_metadata_changes(database: Database) -> None:
    service = RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    )
    await service.set_override(
        "llm.temperature",
        0.1,
        scope_type="user",
        scope_id="1001",
        actor_user_id="9000",
        trigger_message_id="temperature",
    )
    first = await service.snapshot(user_id="1001", group_id="2001")
    second = await service.snapshot(user_id="1002", group_id="2001")
    assert first.llm.temperature == 0.1
    assert second.llm.temperature == 0.7


def test_admin_actor_cannot_be_forged_with_dataclass_replace() -> None:
    real = actor(user_id="1001")
    forged = replace(real, is_superuser=True)
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    assert forged.user_id not in settings.superusers


def test_numeric_conversion_rejects_minimum_nan_and_infinity() -> None:
    registry = ConfigRegistry()
    integer = registry.get("planner.max_pending_messages")
    number = registry.get("llm.temperature")
    with pytest.raises(ValueError, match="不能小于"):
        registry.convert(integer, 0)
    for value in ("nan", "inf", "-inf"):
        with pytest.raises(ValueError, match="有限数字"):
            registry.convert(number, value)
    enum_spec = replace(
        registry.get("llm.model"),
        value_type="enum",
        choices=("basic", "advanced"),
    )
    assert registry.convert(enum_spec, "ADVANCED") == "advanced"
    with pytest.raises(ValueError, match="以下值之一"):
        registry.convert(enum_spec, "unregistered")


@pytest.mark.asyncio
async def test_delete_last_override_restores_explicit_environment_value(
    database: Database,
) -> None:
    settings = make_settings(database.url, local_context_event_limit=31)
    service = RuntimeConfigService(settings=settings, database=database)
    await service.set_override(
        "context.local_event_limit",
        45,
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="set",
    )
    deleted = await service.delete_override(
        "context.local_event_limit",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="unset",
    )
    restored = await service.get_effective("context.local_event_limit")
    assert deleted.success
    assert restored.value == 31
    assert restored.source == "env"


@pytest.mark.asyncio
async def test_restart_override_is_pending_before_initialize_and_same_value_is_not(
    database: Database,
) -> None:
    settings = make_settings(database.url, llm_model="startup-model")
    service = RuntimeConfigService(settings=settings, database=database)
    changed = await service.set_override(
        "llm.model",
        "next-model",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="pending",
    )
    assert changed.pending_restart
    assert (await service.get_effective("llm.model")).value == "startup-model"
    assert await service.pending_restart_count() == 1

    active = RuntimeConfigService(settings=settings, database=database)
    await active.initialize()
    repeated = await active.set_override(
        "llm.model",
        "next-model",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="same-value",
    )
    assert repeated.success
    assert not repeated.pending_restart
    assert await active.pending_restart_count() == 0

    deleted = await active.delete_override(
        "llm.model",
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="remove-restart-override",
    )
    assert deleted.success
    assert deleted.before == "next-model"
    assert deleted.after == "startup-model"
    assert deleted.pending_restart
    assert (await active.get_effective("llm.model")).value == "next-model"
    assert await active.pending_restart_count() == 1


@pytest.mark.asyncio
async def test_two_admin_service_facades_share_atomic_versions(database: Database) -> None:
    settings = make_settings(database.url, superusers_csv="9000,9001")
    first = RuntimeConfigService(settings=settings, database=database)
    second = RuntimeConfigService(settings=settings, database=database)
    same_key = await asyncio.gather(
        first.set_override(
            "planner.max_pending_messages",
            4,
            scope_type="global",
            scope_id="",
            actor_user_id="9000",
            trigger_message_id="admin-a",
        ),
        second.set_override(
            "planner.max_pending_messages",
            5,
            scope_type="global",
            scope_id="",
            actor_user_id="9001",
            trigger_message_id="admin-b",
        ),
    )
    assert sorted(item.version for item in same_key) == [1, 2]
    different_keys = await asyncio.gather(
        first.set_override(
            "context.local_event_limit",
            40,
            scope_type="global",
            scope_id="",
            actor_user_id="9000",
            trigger_message_id="different-a",
        ),
        second.set_override(
            "memory.max_referenced_targets",
            4,
            scope_type="global",
            scope_id="",
            actor_user_id="9001",
            trigger_message_id="different-b",
        ),
    )
    assert all(item.success and item.version == 1 for item in different_keys)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_column", "invalid_value"),
    (("value_type", "python"), ("apply_mode", "shell")),
)
async def test_runtime_override_database_enforces_registered_enums(
    database: Database,
    invalid_column: str,
    invalid_value: str,
) -> None:
    columns = {
        "value_type": ("python", "hot"),
        "apply_mode": ("integer", "shell"),
    }
    value_type, apply_mode = columns[invalid_column]
    assert invalid_value in {value_type, apply_mode}
    statement = text(
        """
        INSERT INTO runtime_config_overrides
        (config_key, scope_type, scope_id, value_json, value_type, apply_mode,
         version, created_at, updated_at, updated_by)
        VALUES
        ('test.invalid', 'global', '', '1', :value_type, :apply_mode,
         1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '9000')
        """
    )
    with pytest.raises(IntegrityError):
        async with database.sessions() as session, session.begin():
            await session.execute(
                statement,
                {"value_type": value_type, "apply_mode": apply_mode},
            )


@pytest.mark.asyncio
async def test_forged_tool_runtime_is_rejected_by_capability_gateway(
    database: Database,
) -> None:
    _runtime, capabilities = admin_stack(database)
    real_message = inbound("把次数改成 10", user_id="1001")
    forged = ToolRuntime(
        inbound=real_message,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:1001",
        trigger_message_id=real_message.message_id,
        actor_user_id="1001",
        actor_is_superuser=True,
        current_group_id=None,
        mentioned_user_ids=(),
    )
    payload = json.loads(
        await capabilities.execute(
            "admin_set_config",
            json.dumps(
                {
                    "key": "planner.max_pending_messages",
                    "value": 10,
                    "scope_type": "global",
                    "scope_id": "",
                }
            ),
            forged,
        )
    )
    assert payload["ok"] is False
    assert payload["error"] == "permission_denied"


@pytest.mark.asyncio
async def test_failed_action_audit_does_not_store_free_text(database: Database) -> None:
    _runtime, capabilities = admin_stack(database)
    message = inbound("add a preference", message_id="redacted-action")
    runtime = ToolRuntime(
        inbound=message,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:9000",
        trigger_message_id=message.message_id,
        actor_user_id="9000",
        actor_is_superuser=True,
        current_group_id=None,
        mentioned_user_ids=(),
    )
    secret_text = "sk-should-never-enter-audit"
    payload = json.loads(
        await capabilities.execute(
            "admin_execute_action",
            json.dumps(
                {
                    "action": "preference.set",
                    "arguments": {
                        "target": "explicit_user_id",
                        "user_id": "87654321",
                        "key": "token",
                        "value": secret_text,
                    },
                }
            ),
            runtime,
        )
    )
    assert payload["ok"] is False
    rows = await AdminAuditService(database).history(limit=10)
    assert secret_text not in json.dumps(
        [{"before": row.before, "after": row.after} for row in rows],
        ensure_ascii=False,
    )
