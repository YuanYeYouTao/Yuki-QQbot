"""Plugin API v2 host application module."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.automation.registry import AutomationCapabilityRegistry
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.admission_adapter import PluginAdmissionSignalAdapter
from qq_ai_bot.plugin_host.audit import PluginAuditService
from qq_ai_bot.plugin_host.automation_adapter import PluginAutomationAdapter
from qq_ai_bot.plugin_host.capability_adapter import PluginCapabilityAdapter
from qq_ai_bot.plugin_host.command_adapter import PluginCommandAdapter
from qq_ai_bot.plugin_host.direct_command_router import DirectCommandRouter
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.emoji_adapter import PluginEmojiSelectionSignalAdapter
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.http_client import SafeHttpClient
from qq_ai_bot.plugin_host.loader import PluginLoader
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.plugin_host.prompt_adapter import PluginPromptAdapter
from qq_ai_bot.plugin_host.repository import (
    PluginAuditRepository,
    PluginConfigRepository,
    PluginInstallationRepository,
    PluginStateRepository,
)
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.plugin_sessions import PluginAgentSessionService
from qq_ai_bot.services.prompt_registry import PromptRegistry
from qq_ai_bot.settings_domains import PluginSettings

InvocationScope = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class PluginBundle:
    installations: PluginInstallationRepository
    config_values: PluginConfigRepository
    state: PluginStateRepository
    audit_repository: PluginAuditRepository
    audit: PluginAuditService
    session_repository: PluginAgentSessionRepository
    sessions: PluginAgentSessionService
    http: SafeHttpClient
    events: PluginEventBus
    extensions: ExtensionRegistry
    emoji_signals: PluginEmojiSelectionSignalAdapter
    prompts: PluginPromptAdapter
    automation: PluginAutomationAdapter
    manager: PluginManager
    tools: PluginCapabilityAdapter
    direct_commands: DirectCommandRouter
    commands: PluginCommandAdapter
    admission_signals: PluginAdmissionSignalAdapter


class PluginModule:
    def __init__(
        self,
        *,
        settings: PluginSettings,
        superusers: frozenset[str],
        yuki_version: str,
        database: Database,
        models: ModelExecutor,
        concurrency: ConcurrencyManager,
        runtime_config: RuntimeConfigService,
        prompt_registry: PromptRegistry,
        automation_registry: AutomationCapabilityRegistry,
        context_factory: Callable[..., Any],
        plugin_invocation_scope: InvocationScope,
        automation_invocation_scope: InvocationScope,
        signal_invocation_scope: InvocationScope,
        on_activated: Callable[..., Any],
        on_deactivated: Callable[..., Any],
        bot_display_name: str = "Yuki",
    ) -> None:
        self._settings = settings
        self._superusers = superusers
        self._yuki_version = yuki_version
        self._database = database
        self._models = models
        self._concurrency = concurrency
        self._runtime_config = runtime_config
        self._prompt_registry = prompt_registry
        self._automation_registry = automation_registry
        self._context_factory = context_factory
        self._plugin_invocation_scope = plugin_invocation_scope
        self._automation_invocation_scope = automation_invocation_scope
        self._signal_invocation_scope = signal_invocation_scope
        self._on_activated = on_activated
        self._on_deactivated = on_deactivated
        self._bot_display_name = bot_display_name

    def build(self) -> PluginBundle:
        settings = self._settings
        installations = PluginInstallationRepository(self._database)
        config_values = PluginConfigRepository(self._database)
        state = PluginStateRepository(self._database)
        audit_repository = PluginAuditRepository(self._database)
        audit = PluginAuditService(audit_repository)
        session_repository = PluginAgentSessionRepository(self._database)
        sessions = PluginAgentSessionService(
            model_executor=self._models,
            concurrency=self._concurrency,
            runtime_config=self._runtime_config,
            repository=session_repository,
            bot_display_name=self._bot_display_name,
            max_history_messages=settings.plugin_ai_session_max_history_messages,
        )
        http = SafeHttpClient(
            timeout_seconds=settings.plugin_http_timeout_seconds,
            max_response_bytes=settings.plugin_http_max_response_bytes,
        )
        events = PluginEventBus(default_timeout_seconds=settings.plugin_hook_timeout_seconds)
        extensions = ExtensionRegistry()
        emoji_signals = PluginEmojiSelectionSignalAdapter(
            extensions,
            timeout_seconds=settings.plugin_hook_timeout_seconds,
        )
        prompts = PluginPromptAdapter(extensions, self._prompt_registry)
        automation = PluginAutomationAdapter(
            extensions=extensions,
            automation=self._automation_registry,
            invocation_scope=self._automation_invocation_scope,
        )
        manager = PluginManager(
            enabled=settings.plugin_system_enabled,
            discovery=PluginDiscovery(
                settings.plugin_directory,
                yuki_version=self._yuki_version,
                plugin_api=settings.plugin_api_version,
            ),
            installations=installations,
            loader=PluginLoader(),
            extensions=extensions,
            event_bus=events,
            context_factory=self._context_factory,
            on_activated=self._on_activated,
            on_deactivated=self._on_deactivated,
            audit=audit_repository,
            start_timeout_seconds=settings.plugin_start_timeout_seconds,
            stop_timeout_seconds=settings.plugin_stop_timeout_seconds,
            background_task_limit=settings.plugin_background_task_limit,
            failure_disable_threshold=settings.plugin_failure_disable_threshold,
        )
        tools = PluginCapabilityAdapter(
            registry=extensions,
            installations=installations,
            audit=audit,
            invocation_scope=self._plugin_invocation_scope,
            is_running=lambda plugin_id: plugin_id in manager.running_plugin_ids,
        )
        direct_commands = DirectCommandRouter(
            bindings=settings.plugin_direct_command_bindings,
            registry=extensions,
            manager=manager,
        )
        commands = PluginCommandAdapter(
            manager=manager,
            registry=extensions,
            superusers=self._superusers,
            invocation_scope=self._plugin_invocation_scope,
            direct_commands=direct_commands,
        )
        admission_signals = PluginAdmissionSignalAdapter(
            extensions,
            timeout_seconds=settings.plugin_hook_timeout_seconds,
            invocation_scope=self._signal_invocation_scope,
        )
        return PluginBundle(
            installations=installations,
            config_values=config_values,
            state=state,
            audit_repository=audit_repository,
            audit=audit,
            session_repository=session_repository,
            sessions=sessions,
            http=http,
            events=events,
            extensions=extensions,
            emoji_signals=emoji_signals,
            prompts=prompts,
            automation=automation,
            manager=manager,
            tools=tools,
            direct_commands=direct_commands,
            commands=commands,
            admission_signals=admission_signals,
        )

    @staticmethod
    def register_lifecycle(bundle: PluginBundle, lifecycle: LifecycleRegistry) -> None:
        lifecycle.register("plugin_http", close=bundle.http.close)
        lifecycle.register(
            "plugin_sessions",
            start=bundle.session_repository.delete_ephemeral,
            close=bundle.session_repository.delete_ephemeral,
        )
        lifecycle.register(
            "plugins",
            start=bundle.manager.start,
            close=bundle.manager.stop,
        )
