"""Shared isolated database and service fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from pathlib import Path

import pytest_asyncio

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import OutboundMessage, OutboundSendReceipt
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    ConversationRepository,
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
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.fake import FakePlannerProvider
from qq_ai_bot.planner.models import (
    DeliveryMode,
    PlannerDecision,
    PlannerInput,
    PlannerReasonCode,
    ToolMode,
    ToolSelection,
    TurnPlan,
)
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.services.agent_tools import AgentToolService
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.command_service import CommandService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.processor import DirectPluginCommandResolver, MessageProcessor
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.relationship_evaluator import FakeRelationshipEvaluator
from qq_ai_bot.services.relationship_worker import RelationshipWorker
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
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


def _successful_test_plan(planner_input: PlannerInput) -> TurnPlan:
    """Represent one valid Planner response; provider failures are tested explicitly."""

    available_scopes = (
        tuple(scope.scope_id for scope in planner_input.available_tool_scopes)
        or planner_input.available_tool_categories
    )
    return TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="回应当前真实发送者的消息",
        target_user_ids=(planner_input.current_sender_user_id,),
        delivery_mode=DeliveryMode.NATURAL_MULTI,
        desired_messages=3,
        tool_selection=ToolSelection(
            mode=(ToolMode.READ_ONLY if planner_input.visual_input_present else ToolMode.INHERIT),
            scopes=available_scopes,
        ),
        confidence=1,
        reason_code=PlannerReasonCode.DIRECT_REQUEST,
    )


@dataclass(slots=True)
class Harness:
    settings: Settings
    database: Database
    conversations: ConversationRepository
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
    conversations = ConversationRepository(database)
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
    ledger = EventLedgerRepository(database)
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
    concurrency = ConcurrencyManager(settings.global_llm_concurrency)
    runtime_config = RuntimeConfigService(settings=settings, database=database)
    time_service = TimeContextService(database, default_timezone=settings.default_timezone)
    relationship_worker = RelationshipWorker(
        settings=settings,
        jobs=relationship_jobs,
        relationships=relationships,
        evaluator=FakeRelationshipEvaluator(),
    )
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
        provider=llm,
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
    )
    planner_context = PlannerContextBuilder(
        ledger=ledger,
        relationships=relationships,
    )
    planner = PlannerService(
        provider=FakePlannerProvider(_successful_test_plan),
        observability=PlannerObservability(),
    )
    processor = MessageProcessor(
        settings=settings,
        conversations=conversations,
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
        planner_context=planner_context,
        planner_service=planner,
        ledger=ledger,
        people=profiles,
        memories=memories,
        relationships=relationships,
        relationship_worker=relationship_worker,
        runtime_config=runtime_config,
        vision_service=vision,
        command_service=command_service,
        direct_plugin_commands=direct_plugin_commands,
        turn_observations=RuntimeTurnObservationRepository(database),
    )
    return Harness(
        settings,
        database,
        conversations,
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
