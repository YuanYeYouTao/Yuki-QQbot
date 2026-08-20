"""Shared isolated database and service fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from pathlib import Path

import pytest_asyncio

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    ConversationScopeRepository,
)
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.messages import OutboundMessage, OutboundSendReceipt
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.executor import require_model_executor
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
from qq_ai_bot.services.agent_tools import AgentToolService
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.command_service import CommandService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.effect_gate import ConversationEffectGate
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.processor import DirectPluginCommandResolver, MessageProcessor
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.relationship_evaluator import FakeRelationshipEvaluator
from qq_ai_bot.services.relationship_worker import RelationshipWorker
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.services.user_profiles import UserProfileService
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import VisionService
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.vision.base import VisionProvider
from qq_ai_bot.web.base import WebSearchProvider


class MemorySender:
    """Record outbound messages and optionally fail every send."""

    _message_ids = count(900001)

    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutboundMessage] = []
        self.calls = 0
        self.fail = fail

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic send failure")
        self.messages.append(message)
        return OutboundSendReceipt(
            platform_message_id=str(next(self._message_ids)),
            transport="test",
        )


@dataclass(slots=True)
class Harness:
    settings: Settings
    database: Database
    ledger: EventLedgerRepository
    scoped_events: ScopedEventLedgerUnitOfWork
    conversation_scopes: ConversationScopeRepository
    conversation_rollups: ConversationRollupRepository
    groups: GroupSettingsRepository
    private_users: PrivateUserSettingsRepository
    profiles: UserProfileRepository
    relationships: RelationshipRepository
    relationship_jobs: RelationshipJobRepository
    relationship_worker: RelationshipWorker
    provider: LLMProvider
    concurrency: ConcurrencyManager
    processor: MessageProcessor
    vision: VisionService | None


def make_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "superusers_csv": "9000",
        "enabled_groups_csv": "2001,2002",
        "ignored_bot_users_csv": "7777",
        "llm_provider": "fake",
        "llm_model": "fake-model",
        "model_profiles_file": Path("__test_model_profiles_not_present__.toml"),
        "global_llm_concurrency": 4,
        "per_user_requests_per_minute": 20,
        "per_group_requests_per_minute": 50,
        "daily_chat_message_delay_min_seconds": 0,
        "daily_chat_message_delay_max_seconds": 0,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def build_harness(
    database: Database,
    settings: Settings,
    provider: LLMProvider | None = None,
    *,
    web_provider: WebSearchProvider | None = None,
    vision_provider: VisionProvider | None = None,
    command_service: CommandService | None = None,
    direct_plugin_commands: DirectPluginCommandResolver | None = None,
) -> Harness:
    groups = GroupSettingsRepository(database)
    private_users = PrivateUserSettingsRepository(
        database,
        initial_affection=settings.relationship_initial_affection,
        initial_trust=settings.relationship_initial_trust,
    )
    profiles = UserProfileRepository(
        database,
        initial_affection=settings.relationship_initial_affection,
        initial_trust=settings.relationship_initial_trust,
    )
    user_profiles = UserProfileService(profiles)
    processed_events = ProcessedEventRepository(database)
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
    scoped_events = ScopedEventLedgerUnitOfWork(database, config=rollup_config)
    ledger = EventLedgerRepository(database)
    ledger.set_scoped_writer(scoped_events)
    conversation_scopes = ConversationScopeRepository(database)
    conversation_rollups = ConversationRollupRepository(database, rollup_config)
    memories = MemoryFactService(MemoryFactRepository(database))
    relationships = RelationshipRepository(
        database,
        initial_affection=settings.relationship_initial_affection,
        initial_trust=settings.relationship_initial_trust,
        trust_cap_offset=settings.trust_affection_cap_offset,
        max_affection_auto_delta=settings.affection_max_auto_delta,
        max_trust_auto_delta=settings.trust_max_auto_delta,
    )
    relationship_jobs = RelationshipJobRepository(
        database,
        max_attempts=settings.relationship_max_attempts,
    )
    web_sources = WebSearchSourceRepository(database)
    vision = (
        VisionService(
            provider=vision_provider,
            resolver=MediaResolver(
                max_download_bytes=settings.vision_max_download_bytes,
                timeout_seconds=settings.vision_media_download_timeout_seconds,
            ),
            preprocessor=ImagePreprocessor(
                max_dimension=settings.vision_max_dimension,
                max_pixels=settings.vision_max_pixels,
                max_prepared_bytes=settings.vision_max_prepared_bytes,
                gif_max_frames=8,
            ),
            analyses=MediaAnalysisRepository(database),
            rate_limiter=VisionRateLimiter(),
            emoji_descriptions=EmojiDescriptionRepository(database),
            max_prepared_bytes=settings.vision_max_prepared_bytes,
            global_concurrency=settings.vision_global_concurrency,
            queue_max_pending=settings.vision_queue_max_pending,
            queue_timeout_seconds=settings.vision_queue_timeout_seconds,
        )
        if vision_provider is not None
        else None
    )
    llm = provider or FakeLLMProvider()
    models = require_model_executor(None, provider=llm, model=settings.llm_model or "fake")
    concurrency = ConcurrencyManager(settings.global_llm_concurrency)
    runtime_config = RuntimeConfigService(settings=settings, database=database)
    time_service = TimeContextService(database, default_timezone=settings.default_timezone)
    relationship_worker = RelationshipWorker(
        settings=settings,
        jobs=relationship_jobs,
        relationships=relationships,
        evaluator=FakeRelationshipEvaluator(),
    )
    rollup_service = ConversationRollupService(
        models=models,
        config=rollup_config,
        timeout_seconds=settings.conversation_rollup_model_timeout_seconds,
    )
    turn_coordinator = ConversationTurnCoordinator(
        cancel_replies_on_new_message=settings.reply_sequence_cancel_on_new_message,
        interrupt_autonomous_on_new_message=(
            settings.conversation_interrupt_autonomous_on_new_message
        ),
    )
    effect_gate = ConversationEffectGate()
    agent_tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=memories,
        actions=AgentActionRepository(database),
        relationships=relationships,
        web_provider=web_provider,
        web_sources=web_sources,
        runtime_config=runtime_config,
    )
    chat = ChatService(
        settings=settings,
        model_executor=models,
        concurrency=concurrency,
        ledger=ledger,
        people=profiles,
        memories=memories,
        relationships=relationships,
        tools=agent_tools,
        web_sources=web_sources,
        source_policy=SourceDisplayPolicy(),
        source_renderer=SourceRenderer(),
        runtime_config=runtime_config,
        time_service=time_service,
        rollup_repository=conversation_rollups,
        rollup_service=rollup_service,
        conversation_scopes=conversation_scopes,
        effect_gate=effect_gate,
        turn_coordinator=turn_coordinator,
    )
    processor = MessageProcessor(
        settings=settings,
        ledger=ledger,
        scoped_events=scoped_events,
        conversation_scopes=conversation_scopes,
        conversation_rollups=conversation_rollups,
        effect_gate=effect_gate,
        groups=groups,
        private_users=private_users,
        user_profiles=user_profiles,
        chat=chat,
        deduplication=DeduplicationService(
            processed_events,
            ttl_seconds=settings.processed_event_ttl_seconds,
        ),
        rate_limiter=SlidingWindowRateLimiter(
            per_user=settings.per_user_requests_per_minute,
            per_group=settings.per_group_requests_per_minute,
        ),
        concurrency=concurrency,
        onebot_connected=lambda: True,
        people=profiles,
        memories=memories,
        relationships=relationships,
        relationship_worker=relationship_worker,
        runtime_config=runtime_config,
        vision_service=vision,
        command_service=command_service,
        direct_plugin_commands=direct_plugin_commands,
        turn_coordinator=turn_coordinator,
        turn_observations=RuntimeTurnObservationRepository(database),
    )
    return Harness(
        settings,
        database,
        ledger,
        scoped_events,
        conversation_scopes,
        conversation_rollups,
        groups,
        private_users,
        profiles,
        relationships,
        relationship_jobs,
        relationship_worker,
        llm,
        concurrency,
        processor,
        vision,
    )


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    path = (tmp_path / "test.db").as_posix()
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.create_schema()
    try:
        yield db
    finally:
        await db.close()
