"""Validated SQLite runtime overrides, snapshots, audit history, and rollback."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.admin.audit import AdminAuditService, add_audit_event, event_from_model
from qq_ai_bot.admin.config_registry import ConfigRegistry
from qq_ai_bot.admin.models import (
    AdminActor,
    AdminOperationEvent,
    AgentRuntimeConfig,
    ConfigApplyMode,
    ConfigChangeResult,
    ConfigScopeType,
    ConfigSpec,
    ConfigValue,
    ContextRuntimeConfig,
    ConversationRuntimeConfig,
    EffectiveConfigValue,
    EmojiRuntimeConfig,
    LLMRuntimeConfig,
    MCPRuntimeConfig,
    MemoryRetrievalRuntimeConfig,
    PlannerRuntimeConfig,
    PluginRuntimeConfig,
    RelationshipRuntimeConfig,
    ReplyRuntimeConfig,
    RuntimeConfigSnapshot,
    SpeechRuntimeConfig,
    ToolingRuntimeConfig,
    VisionRuntimeConfig,
    WebRuntimeConfig,
)
from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel


@dataclass(frozen=True, slots=True)
class RuntimeConfigOverrideRecord:
    """Storage-neutral runtime override projection."""

    id: int
    config_key: str
    scope_type: ConfigScopeType
    scope_id: str
    value: ConfigValue
    value_type: str
    apply_mode: ConfigApplyMode
    version: int
    created_at: datetime
    updated_at: datetime
    updated_by: str


def _record(row: RuntimeConfigOverrideModel) -> RuntimeConfigOverrideRecord:
    try:
        decoded: ConfigValue = json.loads(row.value_json)
    except json.JSONDecodeError:
        decoded = None
    return RuntimeConfigOverrideRecord(
        id=row.id,
        config_key=row.config_key,
        scope_type=ConfigScopeType(row.scope_type),
        scope_id=row.scope_id,
        value=decoded,
        value_type=row.value_type,
        apply_mode=ConfigApplyMode(row.apply_mode),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


class RuntimeConfigRepository:
    """Low-level persistence for validated overrides and atomic config audits."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_all(
        self,
        *,
        keys: tuple[str, ...] | None = None,
    ) -> tuple[RuntimeConfigOverrideRecord, ...]:
        statement = select(RuntimeConfigOverrideModel)
        if keys:
            statement = statement.where(RuntimeConfigOverrideModel.config_key.in_(keys))
        async with self._database.sessions() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_record(row) for row in rows)

    async def list_relevant(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
    ) -> tuple[RuntimeConfigOverrideRecord, ...]:
        conditions: list[Any] = [
            RuntimeConfigOverrideModel.scope_type == ConfigScopeType.GLOBAL.value
        ]
        if group_id:
            conditions.append(
                (RuntimeConfigOverrideModel.scope_type == ConfigScopeType.GROUP.value)
                & (RuntimeConfigOverrideModel.scope_id == group_id)
            )
        if user_id:
            conditions.append(
                (RuntimeConfigOverrideModel.scope_type == ConfigScopeType.USER.value)
                & (RuntimeConfigOverrideModel.scope_id == user_id)
            )
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(select(RuntimeConfigOverrideModel).where(or_(*conditions)))
            ).all()
            return tuple(_record(row) for row in rows)

    async def get(
        self,
        *,
        key: str,
        scope_type: ConfigScopeType,
        scope_id: str,
    ) -> RuntimeConfigOverrideRecord | None:
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.config_key == key,
                    RuntimeConfigOverrideModel.scope_type == scope_type.value,
                    RuntimeConfigOverrideModel.scope_id == scope_id,
                )
            )
            return _record(row) if row is not None else None

    async def save_with_audit(
        self,
        *,
        spec: ConfigSpec,
        value: ConfigValue,
        scope_type: ConfigScopeType,
        scope_id: str,
        actor: AdminActor,
        before_state: dict[str, object],
        started: float,
        initial_version: int = 1,
        operation: str = "set_override",
    ) -> tuple[RuntimeConfigOverrideRecord, AdminOperationEvent]:
        now = datetime.now(UTC)
        statement = (
            insert(RuntimeConfigOverrideModel)
            .values(
                config_key=spec.key,
                scope_type=scope_type.value,
                scope_id=scope_id,
                value_json=json.dumps(value, ensure_ascii=False),
                value_type=spec.value_type,
                apply_mode=spec.apply_mode.value,
                version=max(1, initial_version),
                created_at=now,
                updated_at=now,
                updated_by=actor.user_id,
            )
            .on_conflict_do_update(
                index_elements=[
                    RuntimeConfigOverrideModel.config_key,
                    RuntimeConfigOverrideModel.scope_type,
                    RuntimeConfigOverrideModel.scope_id,
                ],
                set_={
                    "value_json": json.dumps(value, ensure_ascii=False),
                    "value_type": spec.value_type,
                    "apply_mode": spec.apply_mode.value,
                    "version": RuntimeConfigOverrideModel.version + 1,
                    "updated_at": now,
                    "updated_by": actor.user_id,
                },
            )
        )
        async with self._database.sessions() as session, session.begin():
            await session.execute(statement)
            row = await session.scalar(
                select(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.config_key == spec.key,
                    RuntimeConfigOverrideModel.scope_type == scope_type.value,
                    RuntimeConfigOverrideModel.scope_id == scope_id,
                )
            )
            if row is None:
                raise RuntimeError("runtime override was not persisted")
            after_state = _override_state(_record(row))
            audit = await add_audit_event(
                session,
                actor=actor,
                capability="runtime_config",
                operation=operation,
                target_type=f"config.{scope_type.value}",
                target_id=spec.key,
                before=before_state,
                after=after_state,
                success=True,
                error_category=None,
                duration_seconds=time.perf_counter() - started,
            )
            return _record(row), event_from_model(audit)

    async def delete_with_audit(
        self,
        *,
        spec: ConfigSpec,
        scope_type: ConfigScopeType,
        scope_id: str,
        actor: AdminActor,
        before: RuntimeConfigOverrideRecord,
        started: float,
        operation: str = "delete_override",
    ) -> AdminOperationEvent:
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                delete(RuntimeConfigOverrideModel).where(
                    RuntimeConfigOverrideModel.config_key == spec.key,
                    RuntimeConfigOverrideModel.scope_type == scope_type.value,
                    RuntimeConfigOverrideModel.scope_id == scope_id,
                )
            )
            audit = await add_audit_event(
                session,
                actor=actor,
                capability="runtime_config",
                operation=operation,
                target_type=f"config.{scope_type.value}",
                target_id=spec.key,
                before=_override_state(before),
                after=_missing_override_state(spec.key, scope_type, scope_id),
                success=True,
                error_category=None,
                duration_seconds=time.perf_counter() - started,
            )
            return event_from_model(audit)


def _missing_override_state(
    key: str,
    scope_type: ConfigScopeType,
    scope_id: str,
) -> dict[str, object]:
    return {
        "key": key,
        "scope_type": scope_type.value,
        "scope_id": scope_id,
        "override_exists": False,
        "value": None,
        "version": None,
    }


def _override_state(record: RuntimeConfigOverrideRecord) -> dict[str, object]:
    return {
        "key": record.config_key,
        "scope_type": record.scope_type.value,
        "scope_id": record.scope_id,
        "override_exists": True,
        "value": record.value,
        "version": record.version,
    }


def _state_value(state: object, key: str) -> object:
    return state.get(key) if isinstance(state, dict) else None


class RuntimeConfigService:
    """Resolve, validate, persist, audit, snapshot, and roll back runtime settings."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        registry: ConfigRegistry | None = None,
        repository: RuntimeConfigRepository | None = None,
        audit: AdminAuditService | None = None,
    ) -> None:
        self._settings = settings
        self.registry = registry or ConfigRegistry()
        self._repository = repository or RuntimeConfigRepository(database)
        self._audit = audit or AdminAuditService(database)
        self._mutation_lock = database.runtime_config_mutation_lock
        self._active_restart: dict[
            tuple[str, ConfigScopeType, str], RuntimeConfigOverrideRecord
        ] = {}
        self._initialized = False

    async def initialize(self) -> None:
        """Activate persisted restart-required overrides for this process."""

        records = await self._repository.list_all()
        self._active_restart = {
            (row.config_key, row.scope_type, row.scope_id): row
            for row in records
            if row.apply_mode is ConfigApplyMode.RESTART_REQUIRED and self._valid_stored_record(row)
        }
        self._initialized = True

    async def startup_settings_updates(self) -> dict[str, object]:
        """Map activated global restart overrides back to long-lived Settings fields."""

        mapping = {
            "llm.model": "llm_model",
            "llm.timeout_seconds": "llm_timeout_seconds",
            "llm.max_retries": "llm_max_retries",
            "global.llm_concurrency": "global_llm_concurrency",
            "web.global_concurrency": "web_global_concurrency",
            "rate_limit.per_user_per_minute": "per_user_requests_per_minute",
            "rate_limit.per_group_per_minute": "per_group_requests_per_minute",
            "vision.enabled": "vision_enabled",
            "vision.base_url": "vision_base_url",
            "vision.model": "vision_model",
            "vision.global_concurrency": "vision_global_concurrency",
            "vision.queue_max_pending": "vision_queue_max_pending",
            "vision.queue_timeout_seconds": "vision_queue_timeout_seconds",
            "vision.media_download_timeout_seconds": ("vision_media_download_timeout_seconds"),
            "vision.timeout_seconds": "vision_timeout_seconds",
            "vision.max_output_tokens": "vision_max_output_tokens",
            "automation.enabled": "automation_enabled",
            "automation.poll_seconds": "automation_poll_seconds",
            "automation.lease_seconds": "automation_lease_seconds",
            "automation.max_active_per_superuser": "automation_max_active_per_superuser",
            "automation.max_active_per_user": "automation_max_active_per_user",
            "automation.max_steps": "automation_max_steps",
            "automation.max_llm_calls_per_run": "automation_max_llm_calls_per_run",
            "automation.max_tool_calls_per_run": "automation_max_tool_calls_per_run",
            "automation.max_messages_per_run": "automation_max_messages_per_run",
            "automation.max_runtime_seconds": "automation_max_runtime_seconds",
            "automation.min_interval_seconds": "automation_min_interval_seconds",
            "automation.default_misfire_grace_seconds": (
                "automation_default_misfire_grace_seconds"
            ),
            "automation.max_consecutive_failures": "automation_max_consecutive_failures",
            "automation.run_retention_days": "automation_run_retention_days",
            "speech.enabled": "speech_enabled",
            "speech.provider": "speech_provider",
            "speech.socket_path": "speech_socket_path",
            "speech.root": "speech_root",
            "genie.data_dir": "genie_data_dir",
        }
        updates: dict[str, object] = {}
        for key, field_name in mapping.items():
            effective = await self.get_effective(key)
            if effective.value is not None:
                updates[field_name] = (
                    Path(str(effective.value))
                    if field_name in {"speech_socket_path", "speech_root", "genie_data_dir"}
                    else effective.value
                )
        return updates

    async def get_effective(
        self,
        key: str,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> EffectiveConfigValue:
        spec = self.registry.get(key)
        if spec.apply_mode is ConfigApplyMode.SECRET or spec.sensitive:
            return EffectiveConfigValue(
                key=spec.key,
                value=None,
                source="protected",
                scope_type=None,
                scope_id="",
                apply_mode=spec.apply_mode,
                configured=bool(spec.default_getter(self._settings)),
            )
        records = await self._repository.list_relevant(
            user_id=user_id,
            group_id=group_id,
        )
        return self._resolve(spec, records, user_id=user_id, group_id=group_id)

    async def set_override(
        self,
        key: str,
        value: object,
        *,
        scope_type: str,
        scope_id: str,
        actor_user_id: str,
        trigger_message_id: str,
        conversation_key: str = "",
    ) -> ConfigChangeResult:
        started = time.perf_counter()
        actor = self._actor(
            actor_user_id,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
        )
        raw_scope = scope_type
        spec: ConfigSpec | None = None
        try:
            spec = self.registry.get(key)
            scope, normalized_scope_id = self._validate_write(
                spec,
                scope_type,
                scope_id,
                actor,
            )
            converted = self.registry.convert(spec, value)
        except (KeyError, PermissionError, ValueError) as exc:
            category = self._error_category(exc)
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="set_override",
                target_type=f"config.{raw_scope[:16]}",
                target_id=spec.key if spec is not None else key[:128],
                before=None,
                after={"attempted": "[REDACTED]"},
                success=False,
                error_category=category,
                duration_seconds=time.perf_counter() - started,
            )
            return ConfigChangeResult(
                success=False,
                key=spec.key if spec is not None else key,
                scope_type=self._safe_scope(scope_type),
                scope_id=scope_id,
                apply_mode=spec.apply_mode if spec is not None else None,
                error_category=category,
                detail=str(exc),
            )

        async with self._mutation_lock:
            try:
                before_effective = await self.get_effective(
                    spec.key,
                    user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                    group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                )
                before_override = await self._repository.get(
                    key=spec.key,
                    scope_type=scope,
                    scope_id=normalized_scope_id,
                )
                await self._validate_cross_key_change(
                    key=spec.key,
                    value=converted,
                    scope_type=scope,
                    scope_id=normalized_scope_id,
                    delete_override=False,
                )
                row, audit = await self._repository.save_with_audit(
                    spec=spec,
                    value=converted,
                    scope_type=scope,
                    scope_id=normalized_scope_id,
                    actor=actor,
                    before_state=(
                        _override_state(before_override)
                        if before_override is not None
                        else _missing_override_state(spec.key, scope, normalized_scope_id)
                    ),
                    started=started,
                )
                pending_restart = (
                    spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                    and converted != before_effective.value
                )
                return ConfigChangeResult(
                    success=True,
                    key=spec.key,
                    scope_type=scope,
                    scope_id=normalized_scope_id,
                    before=before_effective.value,
                    after=converted,
                    apply_mode=spec.apply_mode,
                    pending_restart=pending_restart,
                    change_id=audit.id,
                    version=row.version,
                    detail=self._apply_detail(
                        spec.apply_mode,
                        pending_restart=pending_restart,
                    ),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                category = self._error_category(exc)
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="set_override",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    before=None,
                    after={"attempted": converted},
                    success=False,
                    error_category=category,
                    duration_seconds=time.perf_counter() - started,
                )
                return ConfigChangeResult(
                    success=False,
                    key=spec.key,
                    scope_type=scope,
                    scope_id=normalized_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category=category,
                    detail=str(exc),
                )

    async def delete_override(
        self,
        key: str,
        *,
        scope_type: str,
        scope_id: str,
        actor_user_id: str,
        trigger_message_id: str,
        conversation_key: str = "",
    ) -> ConfigChangeResult:
        started = time.perf_counter()
        actor = self._actor(
            actor_user_id,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
        )
        spec: ConfigSpec | None = None
        try:
            spec = self.registry.get(key)
            scope, normalized_scope_id = self._validate_write(
                spec,
                scope_type,
                scope_id,
                actor,
            )
        except (KeyError, PermissionError, ValueError) as exc:
            category = self._error_category(exc)
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="delete_override",
                target_type=f"config.{scope_type[:16]}",
                target_id=spec.key if spec is not None else key[:128],
                success=False,
                error_category=category,
                duration_seconds=time.perf_counter() - started,
            )
            return ConfigChangeResult(
                False,
                spec.key if spec else key,
                self._safe_scope(scope_type),
                scope_id,
                apply_mode=spec.apply_mode if spec else None,
                error_category=category,
                detail=str(exc),
            )
        async with self._mutation_lock:
            before = await self._repository.get(
                key=spec.key,
                scope_type=scope,
                scope_id=normalized_scope_id,
            )
            if before is None:
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="delete_override",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    success=False,
                    error_category="not_found",
                    duration_seconds=time.perf_counter() - started,
                )
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    normalized_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="not_found",
                    detail="当前作用域没有数据库覆盖值",
                )
            try:
                await self._validate_cross_key_change(
                    key=spec.key,
                    value=None,
                    scope_type=scope,
                    scope_id=normalized_scope_id,
                    delete_override=True,
                )
                audit = await self._repository.delete_with_audit(
                    spec=spec,
                    scope_type=scope,
                    scope_id=normalized_scope_id,
                    actor=actor,
                    before=before,
                    started=started,
                )
                remaining = await self._repository.list_relevant(
                    user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                    group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                )
                after_effective = self._resolve(
                    spec,
                    remaining,
                    user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                    group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                    honor_restart_activation=False,
                )
                active_effective = await self.get_effective(
                    spec.key,
                    user_id=normalized_scope_id if scope is ConfigScopeType.USER else None,
                    group_id=normalized_scope_id if scope is ConfigScopeType.GROUP else None,
                )
                pending_restart = (
                    spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                    and after_effective.value != active_effective.value
                )
                return ConfigChangeResult(
                    True,
                    spec.key,
                    scope,
                    normalized_scope_id,
                    before=before.value,
                    after=after_effective.value,
                    apply_mode=spec.apply_mode,
                    pending_restart=pending_restart,
                    change_id=audit.id,
                    detail=self._apply_detail(
                        spec.apply_mode,
                        pending_restart=pending_restart,
                    ),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                category = self._error_category(exc)
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="delete_override",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    before=_override_state(before),
                    success=False,
                    error_category=category,
                    duration_seconds=time.perf_counter() - started,
                )
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    normalized_scope_id,
                    apply_mode=spec.apply_mode,
                    error_category=category,
                    detail=str(exc),
                )

    async def history(
        self,
        *,
        key: str | None = None,
        actor_user_id: str | None = None,
        limit: int = 20,
    ) -> tuple[AdminOperationEvent, ...]:
        normalized_key = self.registry.get(key).key if key else None
        return await self._audit.history(
            key=normalized_key,
            actor_user_id=actor_user_id,
            capability="runtime_config",
            limit=limit,
        )

    async def rollback(
        self,
        change_id: int,
        *,
        actor_user_id: str,
        trigger_message_id: str = "",
        conversation_key: str = "",
    ) -> ConfigChangeResult:
        started = time.perf_counter()
        actor = self._actor(
            actor_user_id,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
        )
        original = await self._audit.get(change_id)
        fallback_scope = ConfigScopeType.GLOBAL
        if (
            original is None
            or original.capability != "runtime_config"
            or original.operation not in {"set_override", "delete_override"}
            or not original.success
        ):
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="rollback",
                target_type="config",
                target_id=str(change_id),
                success=False,
                error_category="not_rollbackable",
                duration_seconds=time.perf_counter() - started,
            )
            return ConfigChangeResult(
                False,
                "",
                fallback_scope,
                "",
                error_category="not_rollbackable",
                detail="该变更不存在或不属于可恢复的配置修改",
            )
        if original.actor_user_id != actor.user_id:
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="rollback",
                target_type=original.target_type,
                target_id=original.target_id,
                success=False,
                error_category="permission_denied",
                duration_seconds=time.perf_counter() - started,
            )
            return ConfigChangeResult(
                False,
                original.target_id,
                fallback_scope,
                "",
                error_category="permission_denied",
                detail="只能恢复当前管理员本人执行的配置修改",
            )

        before_state = original.before
        after_state = original.after
        try:
            key = str(_state_value(before_state, "key") or original.target_id)
            spec = self.registry.get(key)
            scope = ConfigScopeType(str(_state_value(before_state, "scope_type")))
            scope_id = str(_state_value(before_state, "scope_id") or "")
            self._validate_write(spec, scope.value, scope_id, actor)
        except (KeyError, PermissionError, ValueError) as exc:
            category = self._error_category(exc)
            await self._audit.record(
                actor=actor,
                capability="runtime_config",
                operation="rollback",
                target_type=original.target_type,
                target_id=original.target_id,
                before=original.after,
                after={"change_id": change_id},
                success=False,
                error_category=category,
                duration_seconds=time.perf_counter() - started,
            )
            return ConfigChangeResult(
                False,
                original.target_id,
                fallback_scope,
                "",
                error_category=category,
                detail=str(exc),
            )

        async with self._mutation_lock:
            current = await self._repository.get(
                key=spec.key,
                scope_type=scope,
                scope_id=scope_id,
            )
            if not self._matches_state(current, after_state):
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="rollback",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    before=_override_state(current) if current else None,
                    after={"change_id": change_id},
                    success=False,
                    error_category="rollback_conflict",
                    duration_seconds=time.perf_counter() - started,
                )
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="rollback_conflict",
                    detail="该作用域之后已有其他修改，拒绝覆盖较新的值",
                )

            restore_exists = bool(_state_value(before_state, "override_exists"))
            if restore_exists:
                restore_value = _state_value(before_state, "value")
                try:
                    converted = self.registry.convert(spec, restore_value)
                    await self._validate_cross_key_change(
                        key=spec.key,
                        value=converted,
                        scope_type=scope,
                        scope_id=scope_id,
                        delete_override=False,
                    )
                    prior_version = _state_value(before_state, "version")
                    initial = (
                        int(prior_version) + 1
                        if isinstance(prior_version, int) and not isinstance(prior_version, bool)
                        else 1
                    )
                    row, audit = await self._repository.save_with_audit(
                        spec=spec,
                        value=converted,
                        scope_type=scope,
                        scope_id=scope_id,
                        actor=actor,
                        before_state=(
                            _override_state(current)
                            if current
                            else _missing_override_state(spec.key, scope, scope_id)
                        ),
                        started=started,
                        initial_version=initial,
                        operation=f"rollback:{change_id}",
                    )
                    active_effective = await self.get_effective(
                        spec.key,
                        user_id=scope_id if scope is ConfigScopeType.USER else None,
                        group_id=scope_id if scope is ConfigScopeType.GROUP else None,
                    )
                    pending_restart = (
                        spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                        and converted != active_effective.value
                    )
                    return ConfigChangeResult(
                        True,
                        spec.key,
                        scope,
                        scope_id,
                        before=current.value if current else None,
                        after=converted,
                        apply_mode=spec.apply_mode,
                        pending_restart=pending_restart,
                        change_id=audit.id,
                        version=row.version,
                        detail=self._apply_detail(
                            spec.apply_mode,
                            pending_restart=pending_restart,
                        ),
                    )
                except ValueError as exc:
                    await self._audit.record(
                        actor=actor,
                        capability="runtime_config",
                        operation="rollback",
                        target_type=f"config.{scope.value}",
                        target_id=spec.key,
                        before=_override_state(current) if current else None,
                        after={"change_id": change_id},
                        success=False,
                        error_category="validation_error",
                        duration_seconds=time.perf_counter() - started,
                    )
                    return ConfigChangeResult(
                        False,
                        spec.key,
                        scope,
                        scope_id,
                        apply_mode=spec.apply_mode,
                        error_category="validation_error",
                        detail=str(exc),
                    )
            if current is None:
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="rollback",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    before=None,
                    after={"change_id": change_id},
                    success=False,
                    error_category="rollback_conflict",
                    duration_seconds=time.perf_counter() - started,
                )
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="rollback_conflict",
                    detail="当前覆盖已经不存在",
                )
            try:
                await self._validate_cross_key_change(
                    key=spec.key,
                    value=None,
                    scope_type=scope,
                    scope_id=scope_id,
                    delete_override=True,
                )
            except ValueError as exc:
                await self._audit.record(
                    actor=actor,
                    capability="runtime_config",
                    operation="rollback",
                    target_type=f"config.{scope.value}",
                    target_id=spec.key,
                    before=_override_state(current),
                    after={"change_id": change_id},
                    success=False,
                    error_category="validation_error",
                    duration_seconds=time.perf_counter() - started,
                )
                return ConfigChangeResult(
                    False,
                    spec.key,
                    scope,
                    scope_id,
                    apply_mode=spec.apply_mode,
                    error_category="validation_error",
                    detail=str(exc),
                )
            audit = await self._repository.delete_with_audit(
                spec=spec,
                scope_type=scope,
                scope_id=scope_id,
                actor=actor,
                before=current,
                started=started,
                operation=f"rollback:{change_id}",
            )
            remaining = await self._repository.list_relevant(
                user_id=scope_id if scope is ConfigScopeType.USER else None,
                group_id=scope_id if scope is ConfigScopeType.GROUP else None,
            )
            effective = self._resolve(
                spec,
                remaining,
                user_id=scope_id if scope is ConfigScopeType.USER else None,
                group_id=scope_id if scope is ConfigScopeType.GROUP else None,
                honor_restart_activation=False,
            )
            active_effective = await self.get_effective(
                spec.key,
                user_id=scope_id if scope is ConfigScopeType.USER else None,
                group_id=scope_id if scope is ConfigScopeType.GROUP else None,
            )
            pending_restart = (
                spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                and effective.value != active_effective.value
            )
            return ConfigChangeResult(
                True,
                spec.key,
                scope,
                scope_id,
                before=current.value,
                after=effective.value,
                apply_mode=spec.apply_mode,
                pending_restart=pending_restart,
                change_id=audit.id,
                detail=self._apply_detail(
                    spec.apply_mode,
                    pending_restart=pending_restart,
                ),
            )

    async def pending_restart_count(self) -> int:
        current = {
            (row.config_key, row.scope_type, row.scope_id): row
            for row in await self._repository.list_all()
            if row.apply_mode is ConfigApplyMode.RESTART_REQUIRED and self._valid_stored_record(row)
        }
        keys = set(current) | set(self._active_restart)
        pending = 0
        for key in keys:
            config_key, _scope_type, _scope_id = key
            spec = self.registry.get(config_key)
            current_value = (
                current[key].value if key in current else spec.default_getter(self._settings)
            )
            active_value = (
                self._active_restart[key].value
                if key in self._active_restart
                else spec.default_getter(self._settings)
            )
            if current_value != active_value:
                pending += 1
        return pending

    async def snapshot(
        self,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
    ) -> RuntimeConfigSnapshot:
        records = await self._repository.list_relevant(
            user_id=user_id,
            group_id=group_id,
        )

        def value(key: str) -> ConfigValue:
            spec = self.registry.get(key)
            return self._resolve(
                spec,
                records,
                user_id=user_id,
                group_id=group_id,
            ).value

        def dual(new_key: str, old_key: str) -> ConfigValue:
            new_effective = self._resolve(
                self.registry.get(new_key),
                records,
                user_id=user_id,
                group_id=group_id,
            )
            old_effective = self._resolve(
                self.registry.get(old_key),
                records,
                user_id=user_id,
                group_id=group_id,
            )
            if str(new_effective.source).startswith("runtime:"):
                return new_effective.value
            if str(old_effective.source).startswith("runtime:"):
                return old_effective.value
            return new_effective.value

        delay_min = float(cast(float | int, value("reply.delay_min_seconds")))
        delay_max = float(cast(float | int, value("reply.delay_max_seconds")))
        return RuntimeConfigSnapshot(
            planner=PlannerRuntimeConfig(
                direct_enabled=bool(value("planner.direct_enabled")),
                group_enabled=bool(value("planner.group_enabled")),
                group_debounce_seconds=float(
                    cast(float | int, value("planner.group_debounce_seconds"))
                ),
                temperature=float(cast(float | int, value("planner.temperature"))),
                max_output_tokens=int(cast(int, value("planner.max_output_tokens"))),
                timeout_seconds=float(cast(float | int, value("planner.timeout_seconds"))),
                confidence_threshold=float(
                    cast(float | int, value("planner.confidence_threshold"))
                ),
                reply_necessity_threshold=int(
                    cast(int, value("planner.reply_necessity_threshold"))
                ),
                max_pending_messages=int(cast(int, value("planner.max_pending_messages"))),
                recent_presence_window_seconds=int(
                    cast(int, value("planner.recent_presence_window_seconds"))
                ),
                max_wait_seconds=int(cast(int, value("planner.max_wait_seconds"))),
                interrupt_autonomous_on_new_message=bool(
                    value("planner.interrupt_autonomous_on_new_message")
                ),
                record_runs=bool(value("planner.record_runs")),
                preferred_messages=int(cast(int, value("planner.preferred_messages"))),
            ),
            plugins=PluginRuntimeConfig(
                hook_timeout_seconds=float(
                    cast(float | int, value("plugins.hook_timeout_seconds"))
                ),
                max_prompt_fragment_characters=int(
                    cast(int, value("plugins.max_prompt_fragment_characters"))
                ),
                max_prompt_characters_per_plugin=int(
                    cast(int, value("plugins.max_prompt_characters_per_plugin"))
                ),
                max_total_prompt_characters=int(
                    cast(int, value("plugins.max_total_prompt_characters"))
                ),
            ),
            context=ContextRuntimeConfig(
                local_event_limit=int(cast(int, value("context.local_event_limit"))),
            ),
            memory=MemoryRetrievalRuntimeConfig(
                retrieval_enabled=bool(value("memory.retrieval_enabled")),
                max_referenced_targets=int(cast(int, value("memory.max_referenced_targets"))),
                self_enabled=self._settings.self_memory_enabled,
                lexical_candidate_limit=int(cast(int, value("memory.lexical_candidate_limit"))),
                context_limit_per_entity=int(cast(int, value("memory.context_limit_per_entity"))),
                overview_limit_per_entity=int(cast(int, value("memory.overview_limit_per_entity"))),
                automatic_recall_per_target_limit=int(
                    cast(int, value("memory.automatic_recall_per_target_limit"))
                ),
                automatic_recall_background_limit=int(
                    cast(int, value("memory.automatic_recall_background_limit"))
                ),
                automatic_recall_continuation_limit=int(
                    cast(int, value("memory.automatic_recall_continuation_limit"))
                ),
                automatic_recall_focused_limit=int(
                    cast(int, value("memory.automatic_recall_focused_limit"))
                ),
                automatic_recall_overview_limit=int(
                    cast(int, value("memory.automatic_recall_overview_limit"))
                ),
                always_on_explicit_preference_limit=int(
                    cast(int, value("memory.always_on_explicit_preference_limit"))
                ),
                query_term_limit=int(cast(int, value("memory.query_term_limit"))),
                short_query_fallback_enabled=bool(value("memory.short_query_fallback_enabled")),
                semantic_enabled=bool(value("memory.semantic_enabled")),
                semantic_candidate_limit=int(cast(int, value("memory.semantic_candidate_limit"))),
                semantic_min_similarity=float(
                    cast(float | int, value("memory.semantic_min_similarity"))
                ),
                hybrid_lexical_weight=float(
                    cast(float | int, value("memory.hybrid_lexical_weight"))
                ),
                hybrid_semantic_weight=float(
                    cast(float | int, value("memory.hybrid_semantic_weight"))
                ),
                hybrid_rrf_k=int(cast(int, value("memory.hybrid_rrf_k"))),
                intent_rerank_enabled=bool(value("memory.intent_rerank_enabled")),
                activation_ranking_enabled=bool(value("memory.activation_ranking_enabled")),
                usage_attribution_enabled=bool(value("memory.usage_attribution_enabled")),
                usage_attribution_timeout_seconds=float(
                    cast(float | int, value("memory.usage_attribution_timeout_seconds"))
                ),
                usage_attribution_job_ttl_seconds=float(
                    cast(float | int, value("memory.usage_attribution_job_ttl_seconds"))
                ),
                usage_attribution_queue_limit=int(
                    cast(int, value("memory.usage_attribution_queue_limit"))
                ),
                reinforcement_enabled=bool(value("memory.reinforcement_enabled")),
                recall_receipts_enabled=bool(value("memory.recall_receipts_enabled")),
                activation_half_life_episode_days=float(
                    cast(float | int, value("memory.activation_half_life_episode_days"))
                ),
                activation_half_life_fact_days=float(
                    cast(float | int, value("memory.activation_half_life_fact_days"))
                ),
                activation_half_life_preference_days=float(
                    cast(float | int, value("memory.activation_half_life_preference_days"))
                ),
                activation_half_life_explicit_days=float(
                    cast(float | int, value("memory.activation_half_life_explicit_days"))
                ),
                reinforcement_alpha_background=float(
                    cast(float | int, value("memory.reinforcement_alpha_background"))
                ),
                reinforcement_alpha_continuation=float(
                    cast(float | int, value("memory.reinforcement_alpha_continuation"))
                ),
                reinforcement_alpha_recall=float(
                    cast(float | int, value("memory.reinforcement_alpha_recall"))
                ),
                reinforcement_alpha_verify=float(
                    cast(float | int, value("memory.reinforcement_alpha_verify"))
                ),
                intent_recent_window_days=int(cast(int, value("memory.intent_recent_window_days"))),
                recall_receipt_retention_days=int(
                    cast(int, value("memory.recall_receipt_retention_days"))
                ),
                recall_trace_candidate_limit=int(
                    cast(int, value("memory.recall_trace_candidate_limit"))
                ),
                consolidation_enabled=bool(value("memory.consolidation_enabled")),
                consolidation_candidate_limit=int(
                    cast(int, value("memory.consolidation_candidate_limit"))
                ),
                consolidation_min_relevance=float(
                    cast(float | int, value("memory.consolidation_min_relevance"))
                ),
                consolidation_model_task=str(value("memory.consolidation_model_task")),
                consolidation_max_output_tokens=int(
                    cast(int, value("memory.consolidation_max_output_tokens"))
                ),
                evidence_weight_explicit=float(
                    cast(float | int, value("memory.evidence_weight_explicit"))
                ),
                evidence_weight_self=float(cast(float | int, value("memory.evidence_weight_self"))),
                evidence_weight_group=float(
                    cast(float | int, value("memory.evidence_weight_group"))
                ),
                evidence_weight_third_party=float(
                    cast(float | int, value("memory.evidence_weight_third_party"))
                ),
                evidence_weight_rebuild=float(
                    cast(float | int, value("memory.evidence_weight_rebuild"))
                ),
                authority_cap_explicit=float(
                    cast(float | int, value("memory.authority_cap_explicit"))
                ),
                authority_cap_self=float(cast(float | int, value("memory.authority_cap_self"))),
                authority_cap_group=float(cast(float | int, value("memory.authority_cap_group"))),
                authority_cap_third_party=float(
                    cast(float | int, value("memory.authority_cap_third_party"))
                ),
                maintenance_enabled=bool(value("memory.maintenance_enabled")),
                maintenance_interval_seconds=float(
                    cast(float | int, value("memory.maintenance_interval_seconds"))
                ),
                maintenance_batch_limit=int(cast(int, value("memory.maintenance_batch_limit"))),
                automatic_stale_days=int(cast(int, value("memory.automatic_stale_days"))),
                third_party_stale_days=int(cast(int, value("memory.third_party_stale_days"))),
                contested_stale_days=int(cast(int, value("memory.contested_stale_days"))),
                stale_max_importance=int(cast(int, value("memory.stale_max_importance"))),
                stale_max_confidence=float(cast(float | int, value("memory.stale_max_confidence"))),
            ),
            reply=ReplyRuntimeConfig(
                delay_min_seconds=delay_min,
                delay_max_seconds=delay_max,
                max_qq_message_chars=int(cast(int, value("reply.max_qq_message_chars"))),
                cancel_on_new_message=bool(value("reply.cancel_on_new_message")),
                plan_hard_max_messages=int(
                    cast(int, dual("reply.hard_max_messages", "reply.plan_hard_max_messages"))
                ),
            ),
            llm=LLMRuntimeConfig(
                model=str(value("llm.model") or ""),
                timeout_seconds=float(cast(float | int, value("llm.timeout_seconds"))),
                max_retries=int(cast(int, value("llm.max_retries"))),
                temperature=float(cast(float | int, value("llm.temperature"))),
                max_output_tokens=int(cast(int, value("llm.max_output_tokens"))),
                thinking_enabled=cast(bool | None, value("llm.thinking_enabled")),
            ),
            agent=AgentRuntimeConfig(
                max_tool_calls=int(cast(int, value("agent.max_tool_calls"))),
                max_model_requests=int(cast(int, value("agent.max_model_requests"))),
                tool_result_max_characters=int(
                    cast(int, value("agent.tool_result_max_characters"))
                ),
            ),
            tooling=ToolingRuntimeConfig(
                max_parallel_calls=int(cast(int, value("tooling.max_parallel_calls"))),
                selected_tool_limit=(
                    int(cast(int, value("tooling.selected_tool_limit")))
                    if value("tooling.selected_tool_limit") is not None
                    else None
                ),
                schema_token_budget=(
                    int(cast(int, value("tooling.schema_token_budget")))
                    if value("tooling.schema_token_budget") is not None
                    else None
                ),
                result_token_budget=(
                    int(cast(int, value("tooling.result_token_budget")))
                    if value("tooling.result_token_budget") is not None
                    else None
                ),
                result_item_limit=(
                    int(cast(int, value("tooling.result_item_limit")))
                    if value("tooling.result_item_limit") is not None
                    else None
                ),
                result_artifact_enabled=bool(value("tooling.result_artifact_enabled")),
                result_artifact_retention_seconds=int(
                    cast(int, value("tooling.result_artifact_retention_seconds"))
                ),
            ),
            mcp=MCPRuntimeConfig(
                enabled=bool(value("mcp.enabled")),
                gateway_enabled=bool(value("mcp.gateway_enabled")),
                tool_selection_mode=str(value("mcp.tool_selection_mode")),
                metadata_cache_ttl_seconds=int(cast(int, value("mcp.metadata_cache_ttl_seconds"))),
                connect_timeout_seconds=float(
                    cast(float | int, value("mcp.connect_timeout_seconds"))
                ),
                request_timeout_seconds=float(
                    cast(float | int, value("mcp.request_timeout_seconds"))
                ),
                selected_tool_limit=(
                    int(cast(int, value("mcp.selected_tool_limit")))
                    if value("mcp.selected_tool_limit") is not None
                    else None
                ),
                schema_token_budget=(
                    int(cast(int, value("mcp.schema_token_budget")))
                    if value("mcp.schema_token_budget") is not None
                    else None
                ),
                result_token_budget=(
                    int(cast(int, value("mcp.result_token_budget")))
                    if value("mcp.result_token_budget") is not None
                    else None
                ),
                result_item_limit=(
                    int(cast(int, value("mcp.result_item_limit")))
                    if value("mcp.result_item_limit") is not None
                    else None
                ),
                max_parallel_calls=int(cast(int, value("mcp.max_parallel_calls"))),
                artifact_retention_seconds=int(cast(int, value("mcp.artifact_retention_seconds"))),
            ),
            web=WebRuntimeConfig(
                mode=self._settings.web.mode.value,
                search_max_results=int(cast(int, value("web.search_max_results"))),
                extract_max_results=int(cast(int, value("web.extract_max_results"))),
                max_calls_per_turn=int(cast(int, value("web.max_calls_per_turn"))),
                tool_result_max_characters=int(cast(int, value("web.tool_result_max_characters"))),
                source_retention_days=int(cast(int, value("web.source_retention_days"))),
                source_max_runs_per_conversation=int(
                    cast(int, value("web.source_max_runs_per_conversation"))
                ),
            ),
            relationship=RelationshipRuntimeConfig(
                confidence_threshold=float(
                    cast(float | int, value("relationship.confidence_threshold"))
                ),
                max_auto_delta=int(cast(int, value("relationship.max_auto_delta"))),
                daily_positive_cap=int(cast(int, value("relationship.daily_positive_cap"))),
                daily_negative_cap=int(cast(int, value("relationship.daily_negative_cap"))),
                conflict_preference_min_gap=int(
                    cast(int, value("relationship.conflict_preference_min_gap"))
                ),
                initial_affection=int(cast(int, value("relationship.initial_affection"))),
                initial_trust=int(cast(int, value("relationship.initial_trust"))),
            ),
            vision=VisionRuntimeConfig(
                max_images_per_turn=int(cast(int, value("vision.max_images_per_turn"))),
                max_frames_per_turn=int(cast(int, value("vision.max_frames_per_turn"))),
                gif_max_frames=int(cast(int, value("vision.gif_max_frames"))),
                thinking_enabled=bool(value("vision.thinking_enabled")),
                thinking_budget=int(cast(int, value("vision.thinking_budget"))),
                low_confidence_retry_threshold=float(
                    cast(float | int, value("vision.low_confidence_retry_threshold"))
                ),
                per_user_requests_per_minute=int(
                    cast(int, value("vision.per_user_requests_per_minute"))
                ),
                per_group_requests_per_minute=int(
                    cast(int, value("vision.per_group_requests_per_minute"))
                ),
                analysis_retention_days=int(cast(int, value("vision.analysis_retention_days"))),
            ),
            emoji=EmojiRuntimeConfig(
                enabled=bool(value("emoji.enabled")),
                collection_enabled=bool(value("emoji.collection_enabled")),
                collection_mode=str(value("emoji.collection_mode")),
                collect_private=bool(value("emoji.collect_private")),
                collect_group=bool(value("emoji.collect_group")),
                auto_adopt_enabled=bool(value("emoji.auto_adopt_enabled")),
                auto_adopt_min_confidence=float(
                    cast(float | int, value("emoji.auto_adopt_min_confidence"))
                ),
                pool_capacity=(
                    int(cast(int, value("emoji.pool_capacity")))
                    if value("emoji.pool_capacity") is not None
                    else None
                ),
                replacement_mode=str(value("emoji.replacement_mode")),
                selector_enabled=bool(value("emoji.selector_enabled")),
                selector_candidate_count=int(cast(int, value("emoji.selector_candidate_count"))),
                selector_score_gap=float(cast(float | int, value("emoji.selector_score_gap"))),
                selector_timeout_seconds=float(
                    cast(float | int, value("emoji.selector_timeout_seconds"))
                ),
                max_effects_per_reply=int(cast(int, value("emoji.max_effects_per_reply"))),
                spontaneous_frequency=float(
                    cast(float | int, value("emoji.spontaneous_frequency"))
                ),
                near_duplicate_enabled=bool(value("emoji.near_duplicate_enabled")),
                near_duplicate_distance=int(cast(int, value("emoji.near_duplicate_distance"))),
                same_emoji_cooldown_seconds=int(
                    cast(int, value("emoji.same_emoji_cooldown_seconds"))
                ),
                scope_repeat_cooldown_seconds=int(
                    cast(int, value("emoji.scope_repeat_cooldown_seconds"))
                ),
                cache_retention_days=int(cast(int, value("emoji.cache_retention_days"))),
                worker_batch_size=int(cast(int, value("emoji.worker_batch_size"))),
                worker_poll_seconds=float(cast(float | int, value("emoji.worker_poll_seconds"))),
                worker_lease_seconds=int(cast(int, value("emoji.worker_lease_seconds"))),
                worker_max_attempts=int(cast(int, value("emoji.worker_max_attempts"))),
                worker_retry_delay_seconds=float(
                    cast(float | int, value("emoji.worker_retry_delay_seconds"))
                ),
                analysis_version=str(value("emoji.analysis_version")),
            ),
            speech=SpeechRuntimeConfig(
                enabled=bool(value("speech.enabled")),
                provider=str(value("speech.provider")),
                socket_path=str(value("speech.socket_path")),
                root=str(value("speech.root")),
                genie_data_dir=str(value("genie.data_dir")),
                default_profile=str(value("speech.default_profile") or ""),
                planner_enabled=bool(
                    dual("speech.agent_effects_enabled", "speech.planner_enabled")
                ),
                default_mode=str(value("speech.default_mode")),
                split_sentence=bool(value("speech.split_sentence")),
                max_synthesis_characters=(
                    int(cast(int, value("speech.max_synthesis_characters")))
                    if value("speech.max_synthesis_characters") is not None
                    else None
                ),
                queue_max_pending=(
                    int(cast(int, value("speech.queue_max_pending")))
                    if value("speech.queue_max_pending") is not None
                    else None
                ),
                cache_retention_hours=(
                    int(cast(int, value("speech.cache_retention_hours")))
                    if value("speech.cache_retention_hours") is not None
                    else None
                ),
                private_enabled=bool(value("speech.private_enabled")),
                group_enabled=bool(value("speech.group_enabled")),
                automation_enabled=bool(value("speech.automation_enabled")),
                plugin_enabled=bool(value("speech.plugin_enabled")),
                text_fallback_enabled=bool(value("speech.text_fallback_enabled")),
                spontaneous_frequency=float(
                    cast(float | int, value("speech.spontaneous_frequency"))
                ),
            ),
            conversation=ConversationRuntimeConfig(
                autonomous_enabled=bool(
                    dual("conversation.autonomous_enabled", "planner.group_enabled")
                ),
                autonomous_debounce_seconds=float(
                    cast(
                        float | int,
                        dual(
                            "conversation.autonomous_debounce_seconds",
                            "planner.group_debounce_seconds",
                        ),
                    )
                ),
                autonomous_admission_threshold=int(
                    cast(
                        int,
                        dual(
                            "conversation.autonomous_admission_threshold",
                            "planner.reply_necessity_threshold",
                        ),
                    )
                ),
                autonomous_batch_limit=int(
                    cast(
                        int,
                        dual(
                            "conversation.autonomous_batch_limit",
                            "planner.max_pending_messages",
                        ),
                    )
                ),
                autonomous_presence_window_seconds=int(
                    cast(
                        int,
                        dual(
                            "conversation.autonomous_presence_window_seconds",
                            "planner.recent_presence_window_seconds",
                        ),
                    )
                ),
                interrupt_autonomous_on_new_message=bool(
                    dual(
                        "conversation.interrupt_autonomous_on_new_message",
                        "planner.interrupt_autonomous_on_new_message",
                    )
                ),
            ),
        )

    def _resolve(
        self,
        spec: ConfigSpec,
        records: tuple[RuntimeConfigOverrideRecord, ...],
        *,
        user_id: str | None,
        group_id: str | None,
        honor_restart_activation: bool = True,
    ) -> EffectiveConfigValue:
        selected_records = records
        if honor_restart_activation and spec.apply_mode is ConfigApplyMode.RESTART_REQUIRED:
            selected_records = tuple(self._active_restart.values())
        by_scope = {
            (row.config_key, row.scope_type, row.scope_id): row
            for row in selected_records
            if row.config_key == spec.key and self._valid_stored_record(row)
        }
        candidates = (
            (
                ConfigScopeType.USER,
                user_id,
            ),
            (
                ConfigScopeType.GROUP,
                group_id,
            ),
            (
                ConfigScopeType.GLOBAL,
                "",
            ),
        )
        for scope, scope_id in candidates:
            if scope_id is None or scope not in spec.allowed_scopes:
                continue
            row = by_scope.get((spec.key, scope, scope_id))
            if row is not None:
                # Pending is computed process-wide by pending_restart_count; an activated
                # row itself is never pending for the value returned here.
                return EffectiveConfigValue(
                    key=spec.key,
                    value=row.value,
                    source=f"runtime:{scope.value}",
                    scope_type=scope,
                    scope_id=scope_id,
                    apply_mode=spec.apply_mode,
                    pending_restart=False,
                )
        source = (
            "env"
            if any(field in self._settings.model_fields_set for field in spec.settings_fields)
            else "default"
        )
        return EffectiveConfigValue(
            key=spec.key,
            value=spec.default_getter(self._settings),
            source=source,
            scope_type=None,
            scope_id="",
            apply_mode=spec.apply_mode,
        )

    def _valid_stored_record(self, row: RuntimeConfigOverrideRecord) -> bool:
        spec = self.registry.maybe_get(row.config_key)
        if (
            spec is None
            or not spec.mutable
            or row.scope_type not in spec.allowed_scopes
            or row.value_type != spec.value_type
            or row.apply_mode is not spec.apply_mode
        ):
            return False
        try:
            return self.registry.convert(spec, row.value) == row.value
        except ValueError:
            return False

    def _validate_write(
        self,
        spec: ConfigSpec,
        scope_type: str,
        scope_id: str,
        actor: AdminActor,
    ) -> tuple[ConfigScopeType, str]:
        if not actor.is_superuser or actor.user_id not in self._settings.superusers:
            raise PermissionError("只有当前真实超级管理员可以修改运行时配置")
        if not spec.mutable:
            if spec.apply_mode is ConfigApplyMode.SECRET:
                raise PermissionError("凭证只能确认是否配置，不能读取或修改")
            raise PermissionError("该配置只能通过启动环境维护")
        try:
            scope = ConfigScopeType(scope_type.casefold())
        except ValueError as exc:
            raise ValueError("scope_type 必须是 global、group 或 user") from exc
        normalized_scope_id = scope_id.strip()
        if scope is ConfigScopeType.GLOBAL:
            if normalized_scope_id:
                raise ValueError("global 作用域的 scope_id 必须为空")
        elif not normalized_scope_id:
            raise ValueError("group/user 作用域必须提供 scope_id")
        if scope not in spec.allowed_scopes:
            allowed = "、".join(item.value for item in spec.allowed_scopes)
            raise ValueError(f"该配置只允许以下作用域：{allowed}")
        return scope, normalized_scope_id

    def _actor(
        self,
        actor_user_id: str,
        *,
        trigger_message_id: str,
        conversation_key: str,
    ) -> AdminActor:
        return AdminActor(
            user_id=actor_user_id,
            is_superuser=actor_user_id in self._settings.superusers,
            trigger_message_id=trigger_message_id,
            conversation_key=conversation_key,
        )

    async def _validate_cross_key_change(
        self,
        *,
        key: str,
        value: ConfigValue,
        scope_type: ConfigScopeType,
        scope_id: str,
        delete_override: bool,
    ) -> None:
        if key not in {"reply.delay_min_seconds", "reply.delay_max_seconds"}:
            return
        records = list(
            await self._repository.list_all(
                keys=("reply.delay_min_seconds", "reply.delay_max_seconds")
            )
        )
        records = [
            row
            for row in records
            if not (
                row.config_key == key and row.scope_type is scope_type and row.scope_id == scope_id
            )
        ]
        if not delete_override:
            spec = self.registry.get(key)
            records.append(
                RuntimeConfigOverrideRecord(
                    id=0,
                    config_key=key,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    value=value,
                    value_type=spec.value_type,
                    apply_mode=spec.apply_mode,
                    version=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    updated_by="validation",
                )
            )
        user_ids = {row.scope_id for row in records if row.scope_type is ConfigScopeType.USER}
        group_ids = {row.scope_id for row in records if row.scope_type is ConfigScopeType.GROUP}
        combinations = {
            (None, None),
            *((user_id, None) for user_id in user_ids),
            *((None, group_id) for group_id in group_ids),
            *((user_id, group_id) for user_id in user_ids for group_id in group_ids),
        }
        min_spec = self.registry.get("reply.delay_min_seconds")
        max_spec = self.registry.get("reply.delay_max_seconds")
        for user_id, group_id in combinations:
            minimum = float(
                cast(
                    float | int,
                    self._resolve(
                        min_spec,
                        tuple(records),
                        user_id=user_id,
                        group_id=group_id,
                    ).value,
                )
            )
            maximum = float(
                cast(
                    float | int,
                    self._resolve(
                        max_spec,
                        tuple(records),
                        user_id=user_id,
                        group_id=group_id,
                    ).value,
                )
            )
            if minimum > maximum:
                raise ValueError("reply.delay_min_seconds 不能大于 reply.delay_max_seconds")

    @staticmethod
    def _matches_state(
        current: RuntimeConfigOverrideRecord | None,
        state: object,
    ) -> bool:
        expected_exists = bool(_state_value(state, "override_exists"))
        if not expected_exists:
            return current is None
        if current is None:
            return False
        return current.value == _state_value(state, "value") and current.version == _state_value(
            state, "version"
        )

    @staticmethod
    def _safe_scope(value: str) -> ConfigScopeType:
        try:
            return ConfigScopeType(value.casefold())
        except ValueError:
            return ConfigScopeType.GLOBAL

    @staticmethod
    def _error_category(exc: Exception) -> str:
        if isinstance(exc, KeyError):
            return "unknown_key"
        if isinstance(exc, PermissionError):
            return "permission_denied"
        if isinstance(exc, ValueError):
            return "validation_error"
        return type(exc).__name__[:64]

    @staticmethod
    def _apply_detail(
        mode: ConfigApplyMode,
        *,
        pending_restart: bool = True,
    ) -> str:
        if mode is ConfigApplyMode.HOT:
            return "已保存并立即生效"
        if mode is ConfigApplyMode.FUTURE_ONLY:
            return "已保存，只影响之后新建的记录或任务"
        if mode is ConfigApplyMode.RESTART_REQUIRED:
            return (
                "已保存，重启 Bot 后生效" if pending_restart else "已保存；有效值未变化，无需重启"
            )
        return "已保存"
