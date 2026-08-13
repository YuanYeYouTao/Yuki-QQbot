"""Application resource container and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import cast

from nonebot import get_bots
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot import __version__
from qq_ai_bot.admin.action_service import ActionRegistry
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules import (
    AdminModule,
    AutomationModule,
    ConversationModule,
    EmojiModule,
    MCPModule,
    MediaModule,
    ModelRuntimeModule,
    PersistenceModule,
    PluginModule,
    SpeechModule,
    WebModule,
)
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.mcp.admin import MCPCommandHandler
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker
from qq_ai_bot.plugin_host.config import BoundConfigFacade
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind
from qq_ai_bot.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
    ToolRuntimeProjection,
)
from qq_ai_bot.plugin_host.http_client import BoundHttpFacade
from qq_ai_bot.plugin_host.manifest import PluginManifest
from qq_ai_bot.plugin_host.media_artifacts import PluginMediaArtifactStore
from qq_ai_bot.plugin_host.notification_delivery import PluginNotificationOutboxWorker
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.secrets import BoundSecretsFacade
from qq_ai_bot.plugin_host.session_facade import BoundAgentSessionFacade
from qq_ai_bot.plugin_host.storage import BoundStorageFacade
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.command_service import CommandService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.plugin_events import publish_notification
from qq_ai_bot.services.processor import MessageProcessor
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.services.user_profiles import UserProfileService
from qq_ai_bot.services.vision_service import VisionService
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.vision.base import VisionProvider
from yuki_plugin_sdk.events import EventName
from yuki_plugin_sdk.permissions import PluginPermission

logger = logging.getLogger(__name__)


class ApplicationContainer:
    """Own all external resources for the NoneBot application lifespan."""

    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        runtime_config: RuntimeConfigService | None = None,
        vision_provider: VisionProvider | None = None,
    ) -> None:
        self.settings = settings
        self.started_at = time.monotonic()
        self.lifecycle = LifecycleRegistry()
        persistence = PersistenceModule(
            settings,
            lifecycle=self.lifecycle,
            database=database,
            runtime_config=runtime_config,
        ).build()
        self.persistence = persistence
        self.database = persistence.database
        self.runtime_config = persistence.runtime_config
        mcp = MCPModule(settings, self.database, lifecycle=self.lifecycle).build()
        self.mcp_bundle = mcp
        self.mcp_repository = mcp.repository
        self.tool_artifacts = mcp.artifacts
        self.mcp_manager = mcp.manager
        self.mcp_tools = mcp.provider
        self.mcp_commands = MCPCommandHandler(
            self.mcp_manager,
            result_max_characters=(
                settings.mcp_result_token_budget * 4
                if settings.mcp_result_token_budget is not None
                else settings.agent_tool_result_max_characters
            ),
            artifacts=self.tool_artifacts,
            artifact_retention_seconds=settings.mcp_artifact_retention_seconds,
        )
        self.admin_action_registry = ActionRegistry()
        self.permission_catalog = PermissionCatalogService(
            settings=settings,
            config_registry=self.runtime_config.registry,
            action_registry=self.admin_action_registry,
        )
        self.conversations = persistence.conversations
        self.groups = persistence.groups
        self.private_users = persistence.private_users
        self.user_profile_repository = persistence.people
        self.people = self.user_profile_repository
        self.user_profiles = UserProfileService(
            self.user_profile_repository,
            self.runtime_config,
        )
        self.processed_events = persistence.processed_events
        self.ledger = persistence.ledger
        self.memories = persistence.memories
        self.memory_context = persistence.memory_context
        self.memory_index = persistence.memory_index
        self.memory_jobs = persistence.memory_jobs
        self.memory_audit = persistence.memory_audit
        self.memory_metrics = persistence.memory_metrics
        self.memory_embeddings = MemoryEmbeddingRuntime(
            settings=settings,
            database=self.database,
            facts=self.memories,
            retriever=self.memory_context.retriever,
        )
        self.agent_actions = persistence.agent_actions
        self.web_sources = persistence.web_sources
        self.media_analyses = persistence.media_analyses
        self.emoji_descriptions = persistence.emoji_descriptions
        self.emoji_repository = persistence.emoji_repository
        self.planner_runs = persistence.planner_runs
        self.voice_preferences = persistence.voice_preferences
        self.voice_profiles = persistence.voice_profiles
        self.speech_generations = persistence.speech_generations
        self.time_context = TimeContextService(
            self.database,
            default_timezone=settings.default_timezone,
        )
        self.relationships = persistence.relationships
        self.relationship_jobs = persistence.relationship_jobs
        model_runtime = ModelRuntimeModule(
            settings.model_runtime,
            self.database,
            lifecycle=self.lifecycle,
        ).build()
        self.model_runtime = model_runtime
        self.model_profiles = model_runtime.profiles
        self.model_clients = model_runtime.clients
        self.model_invocations = model_runtime.invocations
        self.model_router = model_runtime.router
        self.models = model_runtime.executor
        self.provider = model_runtime.chat_provider
        self.web_bundle = WebModule(settings.web, lifecycle=self.lifecycle).build()
        self.web_provider = self.web_bundle.provider
        media = MediaModule(
            settings=settings.vision,
            emoji_settings=settings.emoji,
            analyses=self.media_analyses,
            emoji_descriptions=self.emoji_descriptions,
            emoji_assets=self.emoji_repository,
            lifecycle=self.lifecycle,
            provider=vision_provider,
        ).build()
        self.media = media
        self.vision_provider = media.provider
        self.media_resolver = media.resolver
        self.image_preprocessor = media.image_preprocessor
        self.vision: VisionService | None = media.vision
        self.emoji_module = EmojiModule(
            settings=settings.emoji,
            conversation_settings=settings.conversation,
            repository=self.emoji_repository,
            analyses=self.media_analyses,
            resolver=self.media_resolver,
            preprocessor=self.image_preprocessor,
            vision_provider=self.vision_provider,
            models=self.models,
            runtime_config=self.runtime_config,
            lifecycle=self.lifecycle,
            bot_display_name=settings.bot_display_name,
        )
        emoji = self.emoji_module.build()
        self.emoji_bundle = emoji
        self.emoji_storage = emoji.storage
        self.emoji_lifecycle = emoji.lifecycle
        self.emoji_collector = emoji.collector
        self.emoji_selector = emoji.selector
        self.emoji_effects = emoji.effects
        self.emoji_worker = emoji.worker
        self.concurrency = ConcurrencyManager(settings.global_llm_concurrency)
        self.turn_coordinator = ConversationTurnCoordinator(
            cancel_replies_on_new_message=settings.reply_sequence_cancel_on_new_message,
            interrupt_autonomous_on_new_message=(
                settings.planner_interrupt_autonomous_on_new_message
            ),
        )
        speech = SpeechModule(
            settings=settings.speech,
            preference_repository=self.voice_preferences,
            profile_repository=self.voice_profiles,
            generation_repository=self.speech_generations,
            turns=self.turn_coordinator,
            runtime_config=self.runtime_config,
            lifecycle=self.lifecycle,
            bot_display_name=settings.bot_display_name,
            bot_voice_name=settings.bot_voice_name,
        ).build()
        self.speech_bundle = speech
        self.voice_preference_service = speech.preferences
        self.speech_paths = speech.paths
        self.speech_cache = speech.cache
        self.genie_worker = speech.worker
        self.speech_provider = speech.provider
        self.speech = speech.service
        self.voice_profile_service = speech.profiles
        self.speech_effects = speech.effects
        self.speech_admin = speech.admin
        self.conversation_module = ConversationModule(
            settings=settings,
            persistence=persistence,
            model_runtime=model_runtime,
            runtime_config=self.runtime_config,
            permission_catalog=self.permission_catalog,
            concurrency=self.concurrency,
            turns=self.turn_coordinator,
            time_service=self.time_context,
            web_provider=self.web_provider,
            emoji_effects=self.emoji_effects,
            speech=self.speech,
            speech_effects=self.speech_effects,
            voice_preferences=self.voice_preference_service,
            memory_embeddings=self.memory_embeddings,
            tool_artifacts=self.tool_artifacts,
            tool_invocations=self.mcp_repository,
        )
        conversation = self.conversation_module.build()
        self.conversation = conversation
        self.prompt_registry = conversation.prompt_registry
        self.planner_observability = conversation.planner_observability
        self.planner_provider = conversation.planner_provider
        self.planner = conversation.planner
        self.planner_context = conversation.planner_context
        self.reply_sequence = conversation.reply_sequence
        self.relationship_evaluator = conversation.relationship_evaluator
        self.deduplication = conversation.deduplication
        self.rate_limiter = conversation.rate_limiter
        self.agent_tools = conversation.agent_tools
        self.plugin_agent_tools = conversation.plugin_agent_tools
        self.chat = conversation.chat
        self.chat.register_tool_provider(self.mcp_tools)
        self.memory_mutations = conversation.memory_mutations
        self.memory_auditor = conversation.memory_auditor
        self.memory_worker = conversation.memory_worker
        self.memory_rebuild_service = conversation.memory_rebuild_service
        self.memory_rebuild_worker = conversation.memory_rebuild_worker
        self.memory_maintenance_worker = conversation.memory_maintenance_worker
        self.memory_reflection_worker = conversation.memory_reflection_worker
        self.memory_self_reflection_worker = conversation.memory_self_reflection_worker
        self.memory_dream_worker = conversation.memory_dream_worker
        self.memory_evidence_compaction_worker = (
            conversation.memory_evidence_compaction_worker
        )
        self.relationship_worker = conversation.relationship_worker
        admin = AdminModule(
            settings=settings,
            database=self.database,
            runtime_config=self.runtime_config,
            action_registry=self.admin_action_registry,
            permission_catalog=self.permission_catalog,
            relationships=self.relationships,
            memories=self.memories,
            memory_context=self.memory_context,
            memory_index=self.memory_index,
            memory_embeddings=self.memory_embeddings,
            memory_audit=self.memory_audit,
            memory_maintenance=self.memory_maintenance_worker,
            groups=self.groups,
            private_users=self.private_users,
            emoji_repository=self.emoji_repository,
            emoji_lifecycle=self.emoji_lifecycle,
            emoji_storage=self.emoji_storage,
            emoji_collector=self.emoji_collector,
            emoji_worker=self.emoji_worker,
            speech_admin=self.speech_admin,
            memory_rebuild=self.memory_rebuild_service,
            memory_mutations=self.memory_mutations,
            ledger=self.ledger,
            memory_self_reflection=self.memory_self_reflection_worker,
            memory_dream=self.memory_dream_worker,
        ).build()
        self.admin = admin
        self.admin_audit = admin.audit
        self.relationship_admin = admin.relationships
        self.memory_admin = admin.memories
        self.preference_admin = admin.preferences
        self.group_admin = admin.groups
        self.private_access_admin = admin.private_access
        self.config_admin = admin.config
        self.emoji_admin = admin.emoji
        self.admin_actions = admin.actions
        self.admin_capabilities = admin.capabilities
        self.chat.set_admin_tools(self.admin_capabilities)
        self.automation_module = AutomationModule(
            settings=settings,
            database=self.database,
            models=self.models,
            concurrency=self.concurrency,
            runtime_config=self.runtime_config,
            time_service=self.time_context,
            ledger=self.ledger,
            memories=self.memories,
            relationships=self.relationships,
            admin_actions=self.admin_actions,
            admin_audit=self.admin_audit,
            agent_actions=self.agent_actions,
            web_provider=self.web_provider,
            emoji_repository=self.emoji_repository,
            emoji_selector=self.emoji_selector,
            emoji_storage=self.emoji_storage,
            speech=self.speech,
            mcp_manager=self.mcp_manager,
            mcp_artifacts=self.tool_artifacts,
            bot_connected=self.bot_account_connected,
        )
        automation = self.automation_module.build()
        self.automation_bundle = automation
        self.automation_repository = automation.repository
        self._automation_handlers = automation.handlers
        self.automation_registry = automation.registry
        self.automation = automation.service
        self.automation_tools = automation.tools
        self.chat.set_automation_tools(self.automation_tools)
        self.automation_executor = automation.executor
        self.automation_worker = automation.worker
        self.mcp_automation_bridge = automation.mcp_bridge
        self._plugin_contexts: dict[str, HostPluginContext] = {}
        self.plugin_notification_repository = PluginNotificationRepository(self.database)
        self.plugin_media_artifacts = PluginMediaArtifactStore(self.database)
        self.plugin_notification_outbox = PluginNotificationOutboxWorker(
            repository=self.plugin_notification_repository,
            artifacts=self.plugin_media_artifacts,
            ledger=self.ledger,
        )
        self.plugin_background_turns = PluginBackgroundTurnWorker(
            repository=self.plugin_notification_repository,
            ledger=self.ledger,
            runtime_config=self.runtime_config,
            planner_context=self.planner_context,
            planner=self.planner,
            chat=self.chat,
            turns=self.turn_coordinator,
        )
        self.plugin_module = PluginModule(
            settings=settings.plugins,
            superusers=settings.superusers,
            yuki_version=__version__,
            database=self.database,
            models=self.models,
            concurrency=self.concurrency,
            runtime_config=self.runtime_config,
            prompt_registry=self.prompt_registry,
            automation_registry=self.automation_registry,
            context_factory=self._create_plugin_context,
            plugin_invocation_scope=self._plugin_invocation_scope,
            automation_invocation_scope=self._plugin_automation_invocation_scope,
            signal_invocation_scope=self._plugin_signal_scope,
            on_activated=self._activate_plugin_extensions,
            on_deactivated=self._deactivate_plugin_extensions,
            bot_display_name=settings.bot_display_name,
        )
        plugins = self.plugin_module.build()
        self.plugins = plugins
        self.plugin_installations = plugins.installations
        self.plugin_config_values = plugins.config_values
        self.plugin_state = plugins.state
        self.plugin_audit_repository = plugins.audit_repository
        self.plugin_audit = plugins.audit
        self.plugin_session_repository = plugins.session_repository
        self.plugin_sessions = plugins.sessions
        self.plugin_http = plugins.http
        self.plugin_events = plugins.events
        self.plugin_extensions = plugins.extensions
        self.plugin_emoji_signals = plugins.emoji_signals
        self.plugin_prompts = plugins.prompts
        self.plugin_automation = plugins.automation
        self.plugin_manager = plugins.manager
        self.plugin_tools = plugins.tools
        self.plugin_direct_commands = plugins.direct_commands
        self.plugin_commands = plugins.commands
        self.plugin_planner_signals = plugins.planner_signals
        self.emoji_collector.set_event_publisher(self.plugin_events)
        self.emoji_lifecycle.set_event_publisher(self.plugin_events)
        self.emoji_selector.set_event_publisher(self.plugin_events)
        self.emoji_effects.set_event_publisher(self.plugin_events)
        self.speech.set_event_publisher(self.plugin_events)
        self.speech_effects.set_event_publisher(self.plugin_events)
        self.voice_profile_service.set_event_publisher(self.plugin_events)
        self.emoji_selector.set_plugin_signals(self.plugin_emoji_signals)
        self.chat.set_plugin_tools(self.plugin_tools)
        self.autonomous_groups = AutonomousGroupService(
            chat=self.chat,
            runtime_config=self.runtime_config,
            planner_context=self.planner_context,
            planner=self.planner,
            turn_coordinator=self.turn_coordinator,
            planner_signals=self.plugin_planner_signals,
        )
        self.command_service = CommandService(
            settings=settings,
            conversations=self.conversations,
            people=self.people,
            memories=self.memories,
            concurrency=self.concurrency,
            onebot_connected=self.onebot_connected,
            runtime_config=self.runtime_config,
            relationship_admin=self.relationship_admin,
            memory_admin=self.memory_admin,
            preference_admin=self.preference_admin,
            group_admin=self.group_admin,
            private_access_admin=self.private_access_admin,
            config_admin=self.config_admin,
            permission_catalog=self.permission_catalog,
            vision_service=self.vision,
            automation_service=self.automation,
            automation_repository=self.automation_repository,
            automation_worker=self.automation_worker,
            turn_coordinator=self.turn_coordinator,
            planner_observability=self.planner_observability,
            planner_repository=self.planner_runs,
            plugin_commands=self.plugin_commands,
            emoji_admin=self.emoji_admin,
            speech_admin=self.speech_admin,
            model_invocations=self.model_invocations,
            mcp_commands=self.mcp_commands,
            memory_rebuild=self.memory_rebuild_service,
        )
        self.processor = MessageProcessor(
            settings=settings,
            conversations=self.conversations,
            groups=self.groups,
            private_users=self.private_users,
            user_profiles=self.user_profiles,
            chat=self.chat,
            deduplication=self.deduplication,
            rate_limiter=self.rate_limiter,
            concurrency=self.concurrency,
            onebot_connected=self.onebot_connected,
            ledger=self.ledger,
            people=self.people,
            memories=self.memories,
            memory_worker=self.memory_worker,
            relationships=self.relationships,
            relationship_worker=self.relationship_worker,
            autonomous_groups=self.autonomous_groups,
            runtime_config=self.runtime_config,
            relationship_admin=self.relationship_admin,
            memory_admin=self.memory_admin,
            preference_admin=self.preference_admin,
            group_admin=self.group_admin,
            private_access_admin=self.private_access_admin,
            config_admin=self.config_admin,
            permission_catalog=self.permission_catalog,
            vision_service=self.vision,
            automation_service=self.automation,
            automation_repository=self.automation_repository,
            automation_worker=self.automation_worker,
            command_service=self.command_service,
            direct_plugin_commands=self.plugin_direct_commands,
            planner_context=self.planner_context,
            planner_service=self.planner,
            turn_coordinator=self.turn_coordinator,
            planner_signals=self.plugin_planner_signals,
            event_publisher=self.plugin_events,
            emoji_collector=self.emoji_collector,
            emoji_worker=self.emoji_worker,
            voice_preferences=self.voice_preference_service,
        )
        self._cleanup_stop = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._register_lifecycle()

    def _create_plugin_context(
        self,
        manifest: PluginManifest,
        permissions: frozenset[PluginPermission],
    ) -> HostPluginContext:
        schema_items = self.plugin_extensions.list(
            plugin_id=manifest.id,
            kind=ExtensionKind.CONFIG_SCHEMA,
        )
        config_schema = (
            cast(type[BaseModel], schema_items[0].registration) if schema_items else None
        )

        def config_factory(
            current_user_id: str | None,
            current_group_id: str | None,
        ) -> BoundConfigFacade:
            return BoundConfigFacade(
                repository=self.plugin_config_values,
                plugin_id=manifest.id,
                approved_permissions=permissions,
                schema=config_schema,
                current_user_id=current_user_id,
                current_group_id=current_group_id,
            )

        def session_factory(invocation: PluginInvocation) -> BoundAgentSessionFacade:
            return BoundAgentSessionFacade(
                service=self.plugin_sessions,
                plugin_id=manifest.id,
                actor_user_id=invocation.actor_user_id,
                current_group_id=invocation.current_group_id,
                approved_permissions=permissions,
            )

        agent_capabilities: set[str] = set()
        if PluginPermission.MESSAGE_HISTORY_READ in permissions:
            agent_capabilities.update({"get_recent_chat_history", "search_chat_history"})
        if PluginPermission.MEMORY_PERSON_READ in permissions:
            agent_capabilities.add("get_person_memories")
        if PluginPermission.MEMORY_GROUP_READ in permissions:
            agent_capabilities.add("get_group_memories")
        if PluginPermission.WEB_SEARCH in permissions:
            agent_capabilities.add("web_search")
        if PluginPermission.WEB_READ in permissions:
            agent_capabilities.add("read_webpage")

        secrets = BoundSecretsFacade(
            plugin_id=manifest.id,
            declared_names=manifest.secrets,
        )
        context = HostPluginContext(
            plugin_id=manifest.id,
            approved_permissions=permissions,
            superuser_ids=self.settings.superusers,
            scheduler_task_limit=min(
                manifest.limits.background_tasks,
                self.settings.plugin_background_task_limit,
            ),
            services=PluginFacadeServices(
                bot_display_name=self.settings.bot_display_name,
                ledger=self.ledger,
                people=self.people,
                groups=self.groups,
                memories=self.memories,
                memory_context=self.memory_context,
                relationships=self.relationships,
                memory_admin=self.memory_admin,
                relationship_admin=self.relationship_admin,
                runtime_config=self.runtime_config,
                agent_runner=self.chat._agent_runner,
                agent_tools=self.plugin_agent_tools,
                agent_capabilities=frozenset(agent_capabilities),
                web_provider=self.web_provider,
                mcp_manager=self.mcp_manager,
                vision=self.vision,
                emoji_repository=self.emoji_repository,
                emoji_collector=self.emoji_collector,
                emoji_selector=self.emoji_selector,
                emoji_lifecycle=self.emoji_lifecycle,
                speech=self.speech,
                voice_profiles=self.voice_profile_service,
                automation=self.automation,
                storage=BoundStorageFacade(
                    repository=self.plugin_state,
                    plugin_id=manifest.id,
                    approved_permissions=permissions,
                    storage_mb=manifest.limits.storage_mb,
                ),
                config_factory=config_factory,
                secrets=secrets,
                http=BoundHttpFacade(
                    client=self.plugin_http,
                    approved_permissions=permissions,
                    allowed_hosts=manifest.network.allowed_hosts,
                    secrets=secrets,
                    http_concurrency=manifest.limits.http_concurrency,
                ),
                agent_sessions_factory=session_factory,
                events=self.plugin_events,
                audit=self.plugin_audit,
                notifications=self.plugin_notification_repository,
                notification_wake=self._wake_plugin_notifications,
                media_artifacts=self.plugin_media_artifacts,
                media_storage_mb=manifest.limits.storage_mb,
            ),
        )
        self._plugin_contexts[manifest.id] = context
        return context

    def _wake_plugin_notifications(self) -> None:
        self.plugin_notification_outbox.wake()
        self.plugin_background_turns.wake()

    def _plugin_invocation_scope(
        self,
        plugin_id: str,
        runtime: ToolRuntimeProjection,
        *,
        web_was_used: bool,
    ) -> object:
        context = self._plugin_contexts.get(plugin_id)
        if context is None:
            raise RuntimeError("plugin is not running")
        return context.invocation_scope(
            plugin_id,
            runtime,
            web_was_used=web_was_used,
        )

    def _plugin_automation_invocation_scope(
        self,
        plugin_id: str,
        invocation: PluginInvocation,
    ) -> AbstractAsyncContextManager[object]:
        context = self._plugin_contexts.get(plugin_id)
        if context is None:
            raise RuntimeError("plugin is not running")
        return context.bind(invocation)

    def _plugin_signal_scope(
        self,
        plugin_id: str,
        message: InboundMessage,
        origin: TurnOrigin,
        runtime: RuntimeConfigSnapshot,
    ) -> AbstractContextManager[object] | AbstractAsyncContextManager[object]:
        context = self._plugin_contexts.get(plugin_id)
        if context is None:
            raise RuntimeError("plugin is not running")
        return context.bind(
            PluginInvocation(
                plugin_id=plugin_id,
                origin=origin,
                actor_user_id=message.sender.user_id,
                bot_user_id=message.bot_user_id or "unknown-bot",
                inbound=message,
                runtime_config=runtime,
            )
        )

    def _activate_plugin_extensions(self, manifest: PluginManifest) -> None:
        self.plugin_prompts.activate(
            manifest.id,
            max_characters=manifest.limits.prompt_characters,
        )
        self.plugin_automation.activate(manifest)

    def _deactivate_plugin_extensions(self, plugin_id: str) -> None:
        self.plugin_prompts.deactivate(plugin_id)
        self.plugin_automation.deactivate(plugin_id)
        self._plugin_contexts.pop(plugin_id, None)

    @classmethod
    async def create(cls, settings: Settings) -> ApplicationContainer:
        """Load restart overrides before constructing long-lived clients and limits."""

        database = Database(settings.database_url)
        runtime_config = RuntimeConfigService(
            settings=settings,
            database=database,
        )
        try:
            await runtime_config.initialize()
            active_settings = settings.model_copy(
                update=await runtime_config.startup_settings_updates()
            )
            return cls(
                active_settings,
                database=database,
                runtime_config=runtime_config,
            )
        except Exception:
            await database.close()
            raise

    def onebot_connected(self) -> bool:
        """Return whether NoneBot currently has at least one connected adapter bot."""

        return bool(get_bots())

    def bot_account_connected(self, bot_user_id: str) -> bool:
        """Return whether the exact bot account delegated by a task is connected."""

        return any(str(getattr(bot, "self_id", "")) == bot_user_id for bot in get_bots().values())

    def _register_lifecycle(self) -> None:
        self.lifecycle.register(
            "memory_embeddings",
            start=self.memory_embeddings.start,
            close=self.memory_embeddings.close,
            health=self.memory_embeddings.health,
        )
        self.lifecycle.register("autonomous_groups", close=self.autonomous_groups.close)
        self.lifecycle.register(
            "plugin_notification_outbox",
            start=self.plugin_notification_outbox.start,
            close=self.plugin_notification_outbox.close,
        )
        self.lifecycle.register(
            "plugin_background_turns",
            start=self.plugin_background_turns.start,
            close=self.plugin_background_turns.close,
        )
        self.plugin_module.register_lifecycle(self.plugins, self.lifecycle)
        self.lifecycle.register("application_event", start=self._publish_started)
        if self.settings.speech_enabled:
            self.lifecycle.register("speech_startup", start=self._start_speech)
        self.lifecycle.register(
            "maintenance",
            start=self._start_cleanup,
            close=self._close_cleanup,
        )
        self.conversation_module.register_workers(self.conversation, self.lifecycle)
        self.emoji_module.register_worker(self.emoji_bundle, self.lifecycle)
        self.automation_module.register_lifecycle(self.automation_bundle, self.lifecycle)

    async def _publish_started(self) -> None:
        await publish_notification(
            self.plugin_events,
            EventName.APPLICATION_STARTED,
            {"version": __version__},
        )

    async def _start_speech(self) -> None:
        try:
            async with asyncio.timeout(self.settings.speech_worker_start_timeout_seconds):
                speech_health = await self.speech.health()
                if speech_health.connected:
                    await publish_notification(
                        self.plugin_events,
                        EventName.SPEECH_WORKER_STARTED,
                        {
                            "ready": speech_health.ready,
                            "japanese_frontend_available": (
                                speech_health.japanese_frontend_available
                            ),
                        },
                    )
                if speech_health.ready and self.settings.speech_default_profile:
                    await self.voice_profile_service.sync_profile_metadata(
                        self.settings.speech_default_profile
                    )
        except (TimeoutError, OSError, RuntimeError, ValueError, LookupError) as exc:
            logger.error("speech_startup_degraded error_category=%s", type(exc).__name__)

    async def _start_cleanup(self) -> None:
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="processed-event-cleanup",
        )

    async def _close_cleanup(self) -> None:
        self._cleanup_stop.set()
        if self._cleanup_task is not None:
            await self._cleanup_task

    async def start(self) -> None:
        """Start maintenance tasks after migrations have run."""

        await self.lifecycle.start()

    async def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.is_set():
            try:
                deleted = await self.processed_events.cleanup_expired()
                if deleted:
                    logger.info("processed_events_cleaned count=%d", deleted)
                runtime = await self.runtime_config.snapshot()
                web_deleted = await self.web_sources.cleanup_expired(
                    retention_days=runtime.web.source_retention_days
                )
                if web_deleted:
                    logger.info("web_source_runs_cleaned count=%d", web_deleted)
                vision_deleted = await self.media_analyses.cleanup_expired()
                if vision_deleted:
                    logger.info("media_analyses_cleaned count=%d", vision_deleted)
                emoji_deleted = await self.emoji_admin.cleanup_expired()
                if emoji_deleted:
                    logger.info("emoji_assets_cleaned count=%d", emoji_deleted)
                automation_runs_deleted = await self.automation_repository.cleanup_runs(
                    before=datetime.now(UTC)
                    - timedelta(days=self.settings.automation_run_retention_days)
                )
                if automation_runs_deleted:
                    logger.info("automation_runs_cleaned count=%d", automation_runs_deleted)
                plugin_state_deleted = await self.plugin_state.cleanup_expired()
                media_artifacts_deleted = await self.plugin_media_artifacts.cleanup()
                if media_artifacts_deleted:
                    logger.info(
                        "plugin_media_artifacts_cleaned count=%d",
                        media_artifacts_deleted,
                    )
                if plugin_state_deleted:
                    logger.info("plugin_state_cleaned count=%d", plugin_state_deleted)
                artifact_deleted = await self.tool_artifacts.cleanup()
                if artifact_deleted:
                    logger.info("tool_artifacts_cleaned count=%d", artifact_deleted)
                plugin_sessions_expired = await self.plugin_session_repository.expire_due()
                if plugin_sessions_expired:
                    logger.info(
                        "plugin_sessions_expired count=%d",
                        plugin_sessions_expired,
                    )
                speech_expired, speech_files = await self.speech.cleanup(runtime=runtime.speech)
                if speech_expired:
                    logger.info(
                        "speech_cache_cleaned rows=%d files=%d",
                        speech_expired,
                        speech_files,
                    )
            except (SQLAlchemyError, OSError, RuntimeError) as exc:
                logger.error("processed_event_cleanup_failed", exc_info=exc)
            try:
                await asyncio.wait_for(
                    self._cleanup_stop.wait(),
                    timeout=self.settings.processed_event_cleanup_seconds,
                )
            except TimeoutError:
                continue

    async def close(self) -> None:
        """Gracefully stop tasks and close provider/database pools."""

        await publish_notification(
            self.plugin_events,
            EventName.APPLICATION_STOPPING,
            {"version": __version__},
        )
        if self.settings.speech_enabled:
            await publish_notification(
                self.plugin_events,
                EventName.SPEECH_WORKER_STOPPED,
                {"reason": "application_stopping"},
            )
        await self.lifecycle.close()


_container: ApplicationContainer | None = None


def set_container(container: ApplicationContainer) -> None:
    """Publish the initialized lifespan container to adapter handlers."""

    global _container
    _container = container


def get_container() -> ApplicationContainer:
    """Return the initialized container or fail clearly during invalid lifecycle use."""

    if _container is None:
        raise RuntimeError("application container is not initialized")
    return _container
