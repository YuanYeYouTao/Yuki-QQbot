"""Permission catalog and event-bound role-resolution tests."""

from __future__ import annotations

import json
from dataclasses import replace
from inspect import signature

import pytest

from qq_ai_bot.admin.action_service import ActionRegistry, ActionSpec
from qq_ai_bot.admin.config_registry import ConfigRegistry
from qq_ai_bot.admin.permission_catalog import (
    CapabilityKind,
    PermissionCatalogService,
    PermissionLevel,
    PermissionResolver,
)
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity


def settings() -> Settings:
    return Settings.model_validate({"SUPERUSERS": "9000,9001"})


def inbound(user_id: str) -> InboundMessage:
    return InboundMessage(
        message_id=f"message-{user_id or 'empty'}",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id),
        text="我能修改什么？",
    )


def test_resolver_uses_only_current_sender_qq_and_never_assigns_reserved_roles() -> None:
    resolver = PermissionResolver(settings())

    assert resolver.resolve(inbound("9000")) is PermissionLevel.SUPERUSER
    assert resolver.source(inbound("9000")) == "SUPERUSERS"
    assert resolver.resolve(inbound("1000")) is PermissionLevel.USER
    assert resolver.source(inbound("1000")) == "default_user"
    assert {resolver.resolve(inbound(user_id)) for user_id in ("1000", "9000", "9001", "9999")} == {
        PermissionLevel.USER,
        PermissionLevel.SUPERUSER,
    }

    with pytest.raises(ValueError, match="sender QQ"):
        resolver.resolve(inbound(""))


def test_query_contract_accepts_no_caller_supplied_user_id_or_role() -> None:
    parameters = signature(PermissionCatalogService.report_for_message).parameters

    assert "message" in parameters
    assert "category" in parameters
    assert "query" in parameters
    assert "user_id" not in parameters
    assert "role" not in parameters


def test_user_report_includes_automation_and_time_self_service_operations() -> None:
    report = PermissionCatalogService(settings=settings()).report_for_message(inbound("1000"))

    assert report.permission_level is PermissionLevel.USER
    assert report.mutable_config_count == 0
    assert report.protected_config_count == 0
    assert report.business_action_count == 0
    assert report.mutating_action_count == 0
    assert report.self_service_operation_count == 37
    assert report.self_service_mutation_count == 17
    assert {descriptor.kind for descriptor in report.capabilities} == {CapabilityKind.COMMAND}
    assert {descriptor.id for descriptor in report.capabilities} == {
        "command:chat.help:self",
        "command:chat.new:self",
        "command:chat.status:self",
        "command:chat.stop:self",
        "command:chat.ping:self",
        "command:identity.whoami:self",
        "command:identity.forgetme:self",
        "command:automation.create:self",
        "command:automation.list:self",
        "command:automation.list_history:self",
        "command:automation.get:self",
        "command:automation.update:self",
        "command:automation.diagnose:self",
        "command:automation.pause:self",
        "command:automation.resume:self",
        "command:automation.cancel:self",
        "command:automation.run_now:self",
        "command:automation.history:self",
        "command:time.get_current:self",
        "command:time.get_timezone:self",
        "command:time.set_timezone:self",
        "command:relationship.get:self",
        "command:relationship.history:self",
        "command:memory.list:self",
        "command:memory.add:self",
        "command:memory.update:self",
        "command:memory.delete:self",
        "command:memory.show:self",
        "command:memory.explain:self",
        "command:memory.history:self",
        "command:memory.conflicts:self",
        "command:memory.correct:self",
        "command:memory.invalidate:self",
        "command:memory.restore:self",
        "command:preference.list:self",
        "command:preference.set:self",
        "command:preference.delete:self",
    }
    assert all(
        "确定性 /ai" in descriptor.description
        for descriptor in report.capabilities
        if descriptor.category in {"relationship", "preference"}
        or descriptor.id
        in {
            "command:memory.list:self",
            "command:memory.add:self",
            "command:memory.update:self",
            "command:memory.delete:self",
        }
    )
    relationship_read = next(
        descriptor
        for descriptor in report.capabilities
        if descriptor.id == "command:relationship.get:self"
    )
    assert relationship_read.target_scopes == ("self", "global_person")


def test_superuser_report_has_exact_registry_counts_and_complete_lists() -> None:
    config_registry = ConfigRegistry()
    action_registry = ActionRegistry()
    report = PermissionCatalogService(
        settings=settings(),
        config_registry=config_registry,
        action_registry=action_registry,
    ).report_for_message(inbound("9000"))

    assert report.permission_level is PermissionLevel.SUPERUSER
    assert report.mutable_config_count == 218
    assert report.protected_config_count == 12
    assert report.business_action_count == 44
    assert report.mutating_action_count == 33
    assert report.self_service_operation_count == 49
    assert report.onebot_gateway_count == 1
    assert len(report.capabilities) == 324

    config_ids = {
        descriptor.id
        for descriptor in report.capabilities
        if descriptor.kind is CapabilityKind.CONFIGURATION
    }
    action_ids = {
        descriptor.id
        for descriptor in report.capabilities
        if descriptor.kind is CapabilityKind.ACTION
    }
    assert config_ids == {f"config:{key}" for key in config_registry.keys}
    assert action_ids == {
        f"action:{spec.name}:any_{spec.target_kind}" for spec in action_registry.list()
    }
    assert "onebot:call_onebot_api:any_public_action" in {
        descriptor.id for descriptor in report.capabilities
    }
    assert "command:memory.self-reflection.run:admin" in {
        descriptor.id for descriptor in report.capabilities
    }


def test_payload_is_grouped_complete_stable_and_never_contains_config_values() -> None:
    service = PermissionCatalogService(settings=settings())
    first = service.report_for_message(inbound("9000")).to_dict()
    second = service.report_for_message(inbound("9000")).to_dict()

    assert first == second
    assert first["counts"] == {
        "total": 324,
        "mutable_configurations": 218,
        "protected_configurations": 12,
        "business_actions": 44,
        "mutating_business_actions": 33,
        "self_service_operations": 49,
        "self_service_mutations": 25,
        "onebot_api_gateways": 1,
    }
    assert set(first["available_apply_modes"]) == {
        "hot",
        "future_only",
        "restart_required",
        "immutable",
        "secret",
    }
    assert set(first["available_scopes"]) >= {"global", "group", "user", "self"}

    groups = first["groups"]
    assert isinstance(groups, dict)
    secret_descriptors = groups["secret"][CapabilityKind.CONFIGURATION.value]
    assert len(secret_descriptors) == 7
    assert all("value" not in descriptor for descriptor in secret_descriptors)
    assert all("configured" not in descriptor for descriptor in secret_descriptors)
    levels = first["permission_levels"]
    assert [level["name"] for level in levels if not level["active"]] == [
        "trusted",
        "moderator",
    ]

    report = service.report_for_message(inbound("9000"))
    compact = report.to_compact_dict()
    rendered = json.dumps(compact, ensure_ascii=False)
    compact_ids = {
        item["id"]
        for kinds in compact["groups"].values()
        for items in kinds.values()
        for item in items
    }
    assert len(rendered) < 41000
    assert compact_ids == {descriptor.id for descriptor in report.capabilities}

    summary = report.to_model_dict("summary")
    full_model_view = report.to_model_dict("full")
    assert summary["transient_internal_reference"] is True
    assert summary["do_not_copy_verbatim_to_user"] is True
    assert "capability_ids" not in summary
    assert len(json.dumps(summary, ensure_ascii=False)) < 4500
    assert len(json.dumps(full_model_view, ensure_ascii=False)) < 15000


def test_focused_model_view_finds_registry_alias_without_full_catalog() -> None:
    report = PermissionCatalogService(settings=settings()).report_for_message(
        inbound("9000"),
        query="自主批次消息数",
    )
    payload = report.to_model_dict("focused")

    assert [descriptor.id for descriptor in report.capabilities] == [
        "config:conversation.autonomous_batch_limit"
    ]
    assert [item["id"] for item in payload["capabilities"]] == [
        "config:conversation.autonomous_batch_limit"
    ]
    assert len(json.dumps(payload, ensure_ascii=False)) < 1200

    english_report = PermissionCatalogService(settings=settings()).report_for_message(
        inbound("9000"),
        query="max pending messages",
    )
    assert [descriptor.id for descriptor in english_report.capabilities] == [
        "config:conversation.autonomous_batch_limit"
    ]

    with pytest.raises(ValueError, match="64"):
        PermissionCatalogService(settings=settings()).report_for_message(
            inbound("9000"),
            query="x" * 65,
        )


def test_deterministic_text_contains_every_capability_and_onebot_scope() -> None:
    report = PermissionCatalogService(settings=settings()).report_for_message(inbound("9000"))
    rendered = report.render_text()

    for descriptor in report.capabilities:
        capability_id = descriptor.id
        if descriptor.kind is CapabilityKind.CONFIGURATION:
            capability_id = capability_id.removeprefix("config:")
        elif descriptor.kind is CapabilityKind.ACTION:
            capability_id = capability_id.removeprefix("action:").rsplit(":any_", maxsplit=1)[0]
        elif descriptor.kind is CapabilityKind.COMMAND:
            capability_id = capability_id.removeprefix("command:").removesuffix(":self")
        elif descriptor.kind is CapabilityKind.ONEBOT:
            capability_id = capability_id.removeprefix("onebot:")
        assert capability_id in rendered

    assert "可修改运行时配置参数：218 项" in rendered
    assert "管理员业务接口：44 项，其中修改型 33 项" in rendered
    assert "NapCat/OneBot 通用全接口网关：1 项" in rendered
    assert "全部公开 action" in rendered
    assert "无 action 白名单或 denylist" in rendered
    assert "不是只限于上面的 44 项应用业务接口" in rendered


def test_category_filter_recomputes_counts_and_preserves_sorted_output() -> None:
    report = PermissionCatalogService(settings=settings()).report_for_message(
        inbound("9000"),
        category="memory",
    )

    assert report.mutable_config_count == 78
    assert report.business_action_count == 5
    assert report.mutating_action_count == 4
    assert report.self_service_operation_count == 23
    assert all(descriptor.category == "memory" for descriptor in report.capabilities)
    assert list(report.capabilities) == sorted(
        report.capabilities,
        key=lambda descriptor: (
            descriptor.category,
            descriptor.kind.value,
            descriptor.id,
        ),
    )


class ExtendedActionRegistry(ActionRegistry):
    def list(self) -> tuple[ActionSpec, ...]:
        return (
            *super().list(),
            ActionSpec(
                "diagnostics.snapshot",
                "读取诊断快照",
                "读取当前运行诊断摘要。",
                "group",
                False,
            ),
        )


class DuplicateActionRegistry(ActionRegistry):
    def list(self) -> tuple[ActionSpec, ...]:
        first = super().list()[0]
        return (*super().list(), first)


def test_injected_registry_entries_appear_without_copying_registry_tables() -> None:
    base = ConfigRegistry()
    custom_config = replace(
        base.get("reply.max_qq_message_chars"),
        key="custom.response_ceiling",
        aliases=(),
        category="custom",
    )
    config_registry = ConfigRegistry((*base.list(), custom_config))
    report = PermissionCatalogService(
        settings=settings(),
        config_registry=config_registry,
        action_registry=ExtendedActionRegistry(),
    ).report_for_message(inbound("9000"))

    assert "config:custom.response_ceiling" in {descriptor.id for descriptor in report.capabilities}
    assert "action:diagnostics.snapshot:any_group" in {
        descriptor.id for descriptor in report.capabilities
    }
    assert report.mutable_config_count == 219
    assert report.business_action_count == 45


def test_duplicate_capability_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate permission capability id"):
        PermissionCatalogService(
            settings=settings(),
            action_registry=DuplicateActionRegistry(),
        )
