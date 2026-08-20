"""Persistence module with an explicit immutable repository bundle."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.metrics import ConversationRollupMetrics
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    ConversationScopeRepository,
)
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.memory.activation import MemoryActivationRepository, MemoryIntentRanker
from qq_ai_bot.memory.audit import MemoryAuditService
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.evidence import MemoryEvidencePolicy, MemoryEvidenceWeights
from qq_ai_bot.memory.fts import SQLiteMemoryFTSIndex
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.receipt import MemoryRecallRepository
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.targets import MemoryTargetResolver
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EmojiDescriptionRepository,
    EventLedgerRepository,
    GroupSettingsRepository,
    MediaAnalysisRepository,
    PrivateUserSettingsRepository,
    ProcessedEventRepository,
    RelationshipJobRepository,
    RelationshipRepository,
    UserProfileRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository
from qq_ai_bot.speech.preference_repository import VoicePreferenceRepository
from qq_ai_bot.speech.repository import SpeechGenerationRepository, VoiceProfileRepository


@dataclass(frozen=True, slots=True)
class PersistenceBundle:
    database: Database
    runtime_config: RuntimeConfigService
    groups: GroupSettingsRepository
    private_users: PrivateUserSettingsRepository
    people: UserProfileRepository
    processed_events: ProcessedEventRepository
    ledger: EventLedgerRepository
    scoped_events: ScopedEventLedgerUnitOfWork
    conversation_scopes: ConversationScopeRepository
    conversation_rollups: ConversationRollupRepository
    conversation_rollup_metrics: ConversationRollupMetrics
    memories: MemoryFactService
    memory_context: MemoryContextService
    memory_index: SQLiteMemoryFTSIndex
    memory_jobs: MemoryJobRepository
    memory_receipts: MemoryRecallRepository
    memory_audit: MemoryAuditService
    memory_metrics: MemoryLifecycleMetrics
    memory_rebuilds: MemoryRebuildRepository
    agent_actions: AgentActionRepository
    web_sources: WebSearchSourceRepository
    media_analyses: MediaAnalysisRepository
    emoji_descriptions: EmojiDescriptionRepository
    emoji_repository: EmojiRepository
    voice_preferences: VoicePreferenceRepository
    voice_profiles: VoiceProfileRepository
    speech_generations: SpeechGenerationRepository
    relationships: RelationshipRepository
    relationship_jobs: RelationshipJobRepository
    turn_observations: RuntimeTurnObservationRepository


class PersistenceModule:
    def __init__(
        self,
        settings: Settings,
        *,
        lifecycle: LifecycleRegistry,
        database: Database | None = None,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._lifecycle = lifecycle
        self._database = database
        self._runtime_config = runtime_config

    def build(self) -> PersistenceBundle:
        settings = self._settings
        database = self._database or Database(settings.database_url)
        self._lifecycle.register("database", close=database.close)
        runtime_config = self._runtime_config or RuntimeConfigService(
            settings=settings,
            database=database,
        )
        initial = {
            "initial_affection": settings.relationship_initial_affection,
            "initial_trust": settings.relationship_initial_trust,
        }
        memory_rebuilds = MemoryRebuildRepository(database)
        people = UserProfileRepository(
            database,
            **initial,
            memory_rebuilds=memory_rebuilds,
        )
        memory_repository = MemoryFactRepository(database)
        memory_metrics = MemoryLifecycleMetrics()
        memories = MemoryFactService(
            memory_repository,
            evidence_policy=MemoryEvidencePolicy(
                MemoryEvidenceWeights(
                    explicit=settings.memory_evidence_weight_explicit,
                    self_report=settings.memory_evidence_weight_self,
                    group_report=settings.memory_evidence_weight_group,
                    third_party=settings.memory_evidence_weight_third_party,
                    rebuild=settings.memory_evidence_weight_rebuild,
                    cap_explicit=settings.memory_authority_cap_explicit,
                    cap_self=settings.memory_authority_cap_self,
                    cap_group=settings.memory_authority_cap_group,
                    cap_third_party=settings.memory_authority_cap_third_party,
                )
            ),
            runtime_config=runtime_config,
            metrics=memory_metrics,
        )
        memory_index = SQLiteMemoryFTSIndex(database)
        memory_activation = MemoryActivationRepository(database)
        memory_receipts = MemoryRecallRepository(database)
        memory_context = MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(people)),
            retriever=MemoryRetriever(
                repository=memory_repository,
                lexical_index=memory_index,
                activation_repository=memory_activation,
                intent_ranker=MemoryIntentRanker(memory_metrics),
                mmr_enabled=settings.memory_mmr_enabled,
                mmr_lambda=settings.memory_mmr_lambda,
                mmr_candidate_pool_size=settings.memory_mmr_candidate_pool_size,
            ),
            facts=memories,
            activation=memory_activation,
            receipts=memory_receipts,
            metrics=memory_metrics,
        )
        rollup_config = RollupPolicyConfig(
            raw_tail_events=settings.conversation_rollup_raw_tail_events,
            raw_tail_characters=settings.conversation_rollup_raw_tail_characters,
            trigger_events=settings.conversation_rollup_trigger_events,
            trigger_characters=settings.conversation_rollup_trigger_characters,
            stop_events=settings.conversation_rollup_stop_events,
            stop_characters=settings.conversation_rollup_stop_characters,
            batch_max_events=settings.conversation_rollup_batch_max_events,
            batch_max_characters=settings.conversation_rollup_batch_max_characters,
            summary_max_characters=settings.conversation_rollup_summary_max_characters,
        )
        rollup_metrics = ConversationRollupMetrics()
        scoped_events = ScopedEventLedgerUnitOfWork(
            database,
            config=rollup_config,
            metrics=rollup_metrics,
        )
        ledger = EventLedgerRepository(database)
        ledger.set_scoped_writer(scoped_events)
        return PersistenceBundle(
            database=database,
            runtime_config=runtime_config,
            groups=GroupSettingsRepository(database),
            private_users=PrivateUserSettingsRepository(database, **initial),
            people=people,
            processed_events=ProcessedEventRepository(database),
            ledger=ledger,
            scoped_events=scoped_events,
            conversation_scopes=ConversationScopeRepository(database),
            conversation_rollups=ConversationRollupRepository(
                database,
                rollup_config,
                metrics=rollup_metrics,
            ),
            conversation_rollup_metrics=rollup_metrics,
            memories=memories,
            memory_context=memory_context,
            memory_index=memory_index,
            memory_jobs=MemoryJobRepository(database),
            memory_receipts=memory_receipts,
            memory_audit=MemoryAuditService(
                memory_repository,
                metrics=memory_metrics,
                settings=settings,
                runtime_config=runtime_config,
                activation=memory_activation,
                receipts=memory_receipts,
            ),
            memory_metrics=memory_metrics,
            memory_rebuilds=memory_rebuilds,
            agent_actions=AgentActionRepository(database),
            web_sources=WebSearchSourceRepository(database),
            media_analyses=MediaAnalysisRepository(database),
            emoji_descriptions=EmojiDescriptionRepository(database),
            emoji_repository=EmojiRepository(database),
            voice_preferences=VoicePreferenceRepository(database),
            voice_profiles=VoiceProfileRepository(database),
            speech_generations=SpeechGenerationRepository(database),
            relationships=RelationshipRepository(
                database,
                **initial,
                trust_cap_offset=settings.trust_affection_cap_offset,
                max_affection_auto_delta=settings.affection_max_auto_delta,
                max_trust_auto_delta=settings.trust_max_auto_delta,
            ),
            relationship_jobs=RelationshipJobRepository(
                database,
                max_attempts=settings.relationship_max_attempts,
            ),
            turn_observations=RuntimeTurnObservationRepository(database),
        )
