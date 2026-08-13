"""Conversation, Planner, context, and memory-worker application module."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.model_runtime import ModelRuntimeBundle
from qq_ai_bot.application.modules.persistence import PersistenceBundle
from qq_ai_bot.capabilities import ToolArtifactWriter
from qq_ai_bot.config import Settings
from qq_ai_bot.emoji.effects import EmojiReplyEffectService
from qq_ai_bot.memory.auditing import (
    MemoryAuditCoordinator,
    SelfMemoryAuditor,
    UserMemoryAuditor,
)
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.dream.service import DreamService
from qq_ai_bot.memory.dream.worker import DreamWorker
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.governance import MemoryGovernanceRepository, MemoryGovernanceWorker
from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.memory.rebuild.worker import MemoryRebuildWorker
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
from qq_ai_bot.memory.self_reflection.worker import SelfReflectionWorker
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.provider import LLMPlannerProvider
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.plugin_host.agent_backend import PluginAgentToolBackend
from qq_ai_bot.services.agent_tools import AgentToolService
from qq_ai_bot.services.chat import ChatService, ToolInvocationRecorder
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.services.prompt_registry import PromptRegistry
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.relationship_evaluator import (
    FakeRelationshipEvaluator,
    LLMRelationshipEvaluator,
    RelationshipEvaluator,
)
from qq_ai_bot.services.relationship_worker import RelationshipWorker
from qq_ai_bot.services.reply_sequence import ReplySequenceManager
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.speech.preference_service import VoicePreferenceService
from qq_ai_bot.speech.reply_effect import VoiceReplyEffectService
from qq_ai_bot.speech.service import SpeechService
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.web.base import WebSearchProvider


@dataclass(frozen=True, slots=True)
class ConversationBundle:
    prompt_registry: PromptRegistry
    planner_observability: PlannerObservability
    planner_provider: LLMPlannerProvider
    planner: PlannerService
    planner_context: PlannerContextBuilder
    reply_sequence: ReplySequenceManager
    relationship_evaluator: RelationshipEvaluator
    deduplication: DeduplicationService
    rate_limiter: SlidingWindowRateLimiter
    agent_tools: AgentToolService
    plugin_agent_tools: PluginAgentToolBackend
    chat: ChatService
    memory_mutations: MemoryMutationService
    memory_auditor: MemoryAuditCoordinator
    memory_worker: MemoryWorker
    memory_rebuild_service: MemoryRebuildService
    memory_rebuild_worker: MemoryRebuildWorker
    memory_maintenance_worker: MemoryMaintenanceWorker
    memory_reflection_worker: MemoryGovernanceWorker
    memory_self_reflection_worker: SelfReflectionWorker
    memory_dream_worker: DreamWorker
    relationship_worker: RelationshipWorker


class ConversationModule:
    def __init__(
        self,
        *,
        settings: Settings,
        persistence: PersistenceBundle,
        model_runtime: ModelRuntimeBundle,
        runtime_config: RuntimeConfigService,
        permission_catalog: PermissionCatalogService,
        concurrency: ConcurrencyManager,
        turns: ConversationTurnCoordinator,
        time_service: TimeContextService,
        web_provider: WebSearchProvider | None,
        emoji_effects: EmojiReplyEffectService,
        speech: SpeechService,
        speech_effects: VoiceReplyEffectService,
        voice_preferences: VoicePreferenceService,
        memory_embeddings: MemoryEmbeddingRuntime,
        tool_artifacts: ToolArtifactWriter | None = None,
        tool_invocations: ToolInvocationRecorder | None = None,
    ) -> None:
        self._settings = settings
        self._persistence = persistence
        self._model_runtime = model_runtime
        self._runtime_config = runtime_config
        self._permission_catalog = permission_catalog
        self._concurrency = concurrency
        self._turns = turns
        self._time_service = time_service
        self._web_provider = web_provider
        self._emoji_effects = emoji_effects
        self._speech = speech
        self._speech_effects = speech_effects
        self._voice_preferences = voice_preferences
        self._memory_embeddings = memory_embeddings
        self._tool_artifacts = tool_artifacts
        self._tool_invocations = tool_invocations

    def build(self) -> ConversationBundle:
        settings = self._settings
        persistence = self._persistence
        models = self._model_runtime.executor
        prompt_registry = PromptRegistry(
            max_fragment_characters=settings.plugin_max_prompt_fragment_characters,
            max_characters_per_plugin=settings.plugin_max_prompt_characters_per_plugin,
            max_total_plugin_characters=settings.plugin_max_total_prompt_characters,
        )
        planner_observability = PlannerObservability()
        planner_provider = LLMPlannerProvider(
            model_executor=models,
            temperature=settings.planner_temperature,
            max_output_tokens=settings.planner_max_output_tokens,
            timeout_seconds=settings.planner_timeout_seconds,
            hard_max_messages=settings.reply_plan_hard_max_messages,
            max_wait_seconds=settings.planner_max_wait_seconds,
            observability=planner_observability,
            prompt_registry=prompt_registry,
            bot_display_name=settings.bot_display_name,
        )
        planner = PlannerService(
            provider=planner_provider,
            observability=planner_observability,
            repository=persistence.planner_runs,
        )
        planner_context = PlannerContextBuilder(
            ledger=persistence.ledger,
            relationships=persistence.relationships,
            speech=self._speech,
            voice_preferences=persistence.voice_preferences,
            planner_runs=persistence.planner_runs,
            bot_display_name=settings.bot_display_name,
            bot_aliases=settings.bot_aliases,
            timezone=settings.default_timezone,
        )
        reply_sequence = ReplySequenceManager(self._turns)
        _route, chat_profile = self._model_runtime.router.route(ModelTask.CHAT_AGENT)
        relationship_evaluator: RelationshipEvaluator
        if chat_profile.provider.casefold() == "fake":
            relationship_evaluator = FakeRelationshipEvaluator()
        else:
            relationship_evaluator = LLMRelationshipEvaluator(
                settings=settings,
                model_executor=models,
                concurrency=self._concurrency,
                runtime_config=self._runtime_config,
            )
        deduplication = DeduplicationService(
            persistence.processed_events,
            ttl_seconds=settings.processed_event_ttl_seconds,
        )
        rate_limiter = SlidingWindowRateLimiter(
            per_user=settings.per_user_requests_per_minute,
            per_group=settings.per_group_requests_per_minute,
        )
        memory_worker = MemoryWorker(
            settings=settings,
            jobs=persistence.memory_jobs,
            facts=persistence.memories,
            ledger=persistence.ledger,
            people=persistence.people,
            model_executor=models,
            concurrency=self._concurrency,
            runtime_config=self._runtime_config,
            candidate_resolver=MemoryConflictCandidateResolver(
                persistence.memories.repository,
                retriever=persistence.memory_context.retriever,
                limit=settings.memory_consolidation_candidate_limit,
            ),
            metrics=persistence.memory_metrics,
        )
        memory_mutations = memory_worker.mutations
        memory_auditor = MemoryAuditCoordinator(
            facts=persistence.memories,
            ledger=persistence.ledger,
            mutations=memory_mutations,
            user_auditor=UserMemoryAuditor(
                models,
                self._concurrency,
                bot_display_name=settings.bot_display_name,
            ),
            self_auditor=SelfMemoryAuditor(
                models,
                self._concurrency,
                bot_display_name=settings.bot_display_name,
            ),
        )
        agent_tools = AgentToolService(
            settings=settings,
            ledger=persistence.ledger,
            memories=persistence.memories,
            memory_context=persistence.memory_context,
            memory_mutations=memory_mutations,
            actions=persistence.agent_actions,
            relationships=persistence.relationships,
            web_provider=self._web_provider,
            web_sources=persistence.web_sources,
            runtime_config=self._runtime_config,
            permission_catalog=self._permission_catalog,
        )
        plugin_agent_tools = PluginAgentToolBackend(agent_tools)
        chat = ChatService(
            settings=settings,
            model_executor=models,
            concurrency=self._concurrency,
            ledger=persistence.ledger,
            people=persistence.people,
            memories=persistence.memories,
            memory_context=persistence.memory_context,
            relationships=persistence.relationships,
            tools=agent_tools,
            web_sources=persistence.web_sources,
            source_policy=SourceDisplayPolicy(),
            source_renderer=SourceRenderer(),
            runtime_config=self._runtime_config,
            time_service=self._time_service,
            prompt_composer=PromptComposer(settings, prompt_registry),
            turn_coordinator=self._turns,
            reply_sequence=reply_sequence,
            emoji_effects=self._emoji_effects,
            speech_effects=self._speech_effects,
            tool_artifacts=self._tool_artifacts,
            tool_invocations=self._tool_invocations,
        )
        memory_rebuild_service = MemoryRebuildService(
            settings=settings,
            repository=persistence.memory_rebuilds,
            ledger=persistence.ledger,
            extractor=memory_worker.extractor,
            processor=memory_worker.processor,
        )
        memory_rebuild_worker = MemoryRebuildWorker(
            memory_rebuild_service,
            interval_seconds=settings.memory_rebuild_worker_interval_seconds,
        )
        memory_maintenance_worker = MemoryMaintenanceWorker(
            settings=settings,
            facts=persistence.memories,
            runtime_config=self._runtime_config,
            metrics=persistence.memory_metrics,
            mutations=memory_mutations,
        )
        memory_reflection_worker = MemoryGovernanceWorker(
            settings=settings,
            repository=MemoryGovernanceRepository(persistence.database),
            facts=persistence.memories,
            mutations=memory_mutations,
            metrics=persistence.memory_metrics,
        )
        self_reflection_repository = SelfReflectionRepository(persistence.database)
        memory_self_reflection_worker = SelfReflectionWorker(
            settings=settings,
            repository=self_reflection_repository,
            service=SelfReflectionService(
                settings=settings,
                repository=self_reflection_repository,
                facts=persistence.memories,
                mutations=memory_mutations,
                models=models,
                concurrency=self._concurrency,
                metrics=persistence.memory_metrics,
            ),
            metrics=persistence.memory_metrics,
        )
        memory_dream_repository = DreamRepository(persistence.database)
        memory_dream_worker = DreamWorker(
            settings=settings,
            repository=memory_dream_repository,
            service=DreamService(
                settings=settings,
                repository=memory_dream_repository,
                facts=persistence.memories,
                mutations=memory_mutations,
                embeddings=self._memory_embeddings,
                models=models,
                concurrency=self._concurrency,
            ),
        )
        relationship_worker = RelationshipWorker(
            settings=settings,
            jobs=persistence.relationship_jobs,
            relationships=persistence.relationships,
            evaluator=relationship_evaluator,
            runtime_config=self._runtime_config,
        )
        return ConversationBundle(
            prompt_registry,
            planner_observability,
            planner_provider,
            planner,
            planner_context,
            reply_sequence,
            relationship_evaluator,
            deduplication,
            rate_limiter,
            agent_tools,
            plugin_agent_tools,
            chat,
            memory_mutations,
            memory_auditor,
            memory_worker,
            memory_rebuild_service,
            memory_rebuild_worker,
            memory_maintenance_worker,
            memory_reflection_worker,
            memory_self_reflection_worker,
            memory_dream_worker,
            relationship_worker,
        )

    @staticmethod
    def register_workers(bundle: ConversationBundle, lifecycle: LifecycleRegistry) -> None:
        lifecycle.register(
            "memory_worker",
            start=bundle.memory_worker.start,
            close=bundle.memory_worker.close,
        )
        lifecycle.register(
            "memory_rebuild_worker",
            start=bundle.memory_rebuild_worker.start,
            close=bundle.memory_rebuild_worker.close,
        )
        lifecycle.register(
            "memory_maintenance_worker",
            start=bundle.memory_maintenance_worker.start,
            close=bundle.memory_maintenance_worker.close,
        )
        lifecycle.register(
            "memory_governance_worker",
            start=bundle.memory_reflection_worker.start,
            close=bundle.memory_reflection_worker.close,
        )
        lifecycle.register(
            "memory_self_reflection_worker",
            start=bundle.memory_self_reflection_worker.start,
            close=bundle.memory_self_reflection_worker.close,
        )
        lifecycle.register(
            "memory_dream_worker",
            start=bundle.memory_dream_worker.start,
            close=bundle.memory_dream_worker.close,
            health=bundle.memory_dream_worker.health,
        )
        lifecycle.register(
            "relationship_worker",
            start=bundle.relationship_worker.start,
            close=bundle.relationship_worker.close,
        )
