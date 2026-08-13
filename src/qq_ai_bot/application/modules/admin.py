"""Administrator business-service application module."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.action_service import ActionRegistry, AdminActionService
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.capabilities import AdminCapabilityService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.config import Settings
from qq_ai_bot.emoji.admin import EmojiAdminService
from qq_ai_bot.emoji.collector import EmojiCollector
from qq_ai_bot.emoji.lifecycle import EmojiLifecycleService
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.emoji.worker import EmojiWorker
from qq_ai_bot.memory.audit import MemoryAuditService
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.dream.worker import DreamWorker
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.memory.self_reflection.worker import SelfReflectionWorker
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    GroupSettingsRepository,
    PrivateUserSettingsRepository,
    RelationshipRepository,
)
from qq_ai_bot.services.admin.config_admin import ConfigAdminService
from qq_ai_bot.services.admin.group_admin import GroupAdminService
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.private_access_admin import PrivateAccessAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from qq_ai_bot.speech.admin import SpeechAdminService


@dataclass(frozen=True, slots=True)
class AdminBundle:
    audit: AdminAuditService
    relationships: RelationshipAdminService
    memories: MemoryAdminService
    preferences: PreferenceAdminService
    groups: GroupAdminService
    private_access: PrivateAccessAdminService
    config: ConfigAdminService
    emoji: EmojiAdminService
    actions: AdminActionService
    capabilities: AdminCapabilityService


class AdminModule:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        runtime_config: RuntimeConfigService,
        action_registry: ActionRegistry,
        permission_catalog: PermissionCatalogService,
        relationships: RelationshipRepository,
        memories: MemoryFactService,
        memory_context: MemoryContextService,
        memory_index: SQLiteMemoryFTSIndex,
        memory_embeddings: MemoryEmbeddingRuntime,
        memory_audit: MemoryAuditService,
        memory_maintenance: MemoryMaintenanceWorker,
        groups: GroupSettingsRepository,
        private_users: PrivateUserSettingsRepository,
        emoji_repository: EmojiRepository,
        emoji_lifecycle: EmojiLifecycleService,
        emoji_storage: EmojiStorage,
        emoji_collector: EmojiCollector,
        emoji_worker: EmojiWorker | None,
        speech_admin: SpeechAdminService,
        memory_rebuild: MemoryRebuildService,
        memory_mutations: MemoryMutationService,
        ledger: EventLedgerRepository,
        memory_self_reflection: SelfReflectionWorker,
        memory_dream: DreamWorker,
    ) -> None:
        self._settings = settings
        self._database = database
        self._runtime_config = runtime_config
        self._action_registry = action_registry
        self._permission_catalog = permission_catalog
        self._relationships = relationships
        self._memories = memories
        self._memory_context = memory_context
        self._memory_index = memory_index
        self._memory_embeddings = memory_embeddings
        self._memory_audit = memory_audit
        self._memory_maintenance = memory_maintenance
        self._groups = groups
        self._private_users = private_users
        self._emoji_repository = emoji_repository
        self._emoji_lifecycle = emoji_lifecycle
        self._emoji_storage = emoji_storage
        self._emoji_collector = emoji_collector
        self._emoji_worker = emoji_worker
        self._speech_admin = speech_admin
        self._memory_rebuild = memory_rebuild
        self._memory_mutations = memory_mutations
        self._ledger = ledger
        self._memory_self_reflection = memory_self_reflection
        self._memory_dream = memory_dream

    def build(self) -> AdminBundle:
        audit = AdminAuditService(self._database)
        relationships = RelationshipAdminService(
            settings=self._settings,
            relationships=self._relationships,
            audit=audit,
            runtime_config=self._runtime_config,
        )
        memories = MemoryAdminService(
            settings=self._settings,
            memories=self._memories,
            audit=audit,
            memory_context=self._memory_context,
            memory_index=self._memory_index,
            memory_embeddings=self._memory_embeddings,
            runtime_config=self._runtime_config,
            fact_audit=self._memory_audit,
            maintenance=self._memory_maintenance,
            mutations=self._memory_mutations,
            ledger=self._ledger,
            self_reflection=self._memory_self_reflection,
            dream=self._memory_dream,
        )
        preferences = PreferenceAdminService(
            settings=self._settings,
            memories=self._memories,
            audit=audit,
            memory_mutations=memories,
        )
        groups = GroupAdminService(
            settings=self._settings,
            groups=self._groups,
            runtime_config=self._runtime_config,
            audit=audit,
        )
        private_access = PrivateAccessAdminService(
            settings=self._settings,
            private_users=self._private_users,
            audit=audit,
            runtime_config=self._runtime_config,
        )
        config = ConfigAdminService(self._runtime_config)
        emoji = EmojiAdminService(
            repository=self._emoji_repository,
            lifecycle=self._emoji_lifecycle,
            storage=self._emoji_storage,
            collector=self._emoji_collector,
            config=config,
            worker=self._emoji_worker,
        )
        actions = AdminActionService(
            settings=self._settings,
            relationships=relationships,
            memories=memories,
            preferences=preferences,
            groups=groups,
            private_access=private_access,
            emoji=emoji,
            speech=self._speech_admin,
            registry=self._action_registry,
        )
        capabilities = AdminCapabilityService(
            settings=self._settings,
            runtime_config=self._runtime_config,
            actions=actions,
            audit=audit,
            permission_catalog=self._permission_catalog,
            memory_rebuild=self._memory_rebuild,
        )
        return AdminBundle(
            audit,
            relationships,
            memories,
            preferences,
            groups,
            private_access,
            config,
            emoji,
            actions,
            capabilities,
        )
