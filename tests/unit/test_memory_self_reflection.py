"""Bounded SELF-reflection scheduling and isolation contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.self_reflection.models import (
    SelfReflectionBatch,
    SelfReflectionCycleResult,
    SelfReflectionHealth,
    SelfReflectionOutput,
)
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
from qq_ai_bot.memory.self_reflection.worker import SelfReflectionWorker
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository, PeopleRepository
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from qq_ai_bot.services.profile_commands import ProfileCommandHandler


class RecordingSelfReflectionService:
    def __init__(self) -> None:
        self.batches: list[SelfReflectionBatch] = []

    async def reflect(self, batch: SelfReflectionBatch) -> tuple[int, int]:
        self.batches.append(batch)
        return 0, 0


class FailingSelfReflectionService:
    def __init__(self) -> None:
        self.calls = 0

    async def reflect(self, batch: SelfReflectionBatch) -> tuple[int, int]:
        del batch
        self.calls += 1
        raise ValueError("invalid reflection input")


class StaticReflectionConcurrency:
    def __init__(self, output: SelfReflectionOutput) -> None:
        self.output = output

    async def run_llm(self, *_args: object, **_kwargs: object) -> SelfReflectionOutput:
        return self.output


class RecordingSelfReflectionWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.result = SelfReflectionCycleResult(
            attempted_batches=1,
            completed_batches=1,
        )

    async def run_now(self) -> SelfReflectionCycleResult:
        self.calls += 1
        return self.result

    async def health(self) -> SelfReflectionHealth:
        return SelfReflectionHealth(
            enabled=True,
            running=True,
            schedule_hours=(4, 12, 20),
            timezone="Asia/Shanghai",
            pending_conversations=2,
            calls_today=1,
        )


@pytest.mark.asyncio
async def test_first_enable_baselines_history_and_only_collects_new_events(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="historical",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="历史消息不应进入自省",
        group_id="3001",
    )
    repository = SelfReflectionRepository(database)

    assert await repository.scan_new_events() == 0
    assert (
        await repository.claim_due(
            scheduled_slot="2026-08-08:04",
            local_date="2026-08-08",
            event_threshold=1,
            character_threshold=1,
            max_wait_seconds=1,
            max_sessions=3,
            max_daily_calls=9,
            max_events=20,
            max_characters=8000,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_reflection_episode_is_bounded_and_requires_yuki_participation(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    for index in range(11):
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"episode-user-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content=f"第 {index} 条真实群消息",
            group_id="3001",
        )
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="episode-yuki",
        scope_type=ScopeType.GROUP,
        sender_user_id="8000",
        direction="outbound",
        content="这是 Yuki 已确认投递的回复",
        group_id="3001",
        sender_is_bot=True,
    )
    assert await repository.scan_new_events() == 12

    episodes = await repository.claim_due(
        scheduled_slot="2026-08-08:12",
        local_date="2026-08-08",
        event_threshold=12,
        character_threshold=6000,
        max_wait_seconds=28800,
        max_sessions=3,
        max_daily_calls=9,
        max_events=20,
        max_characters=8000,
    )

    assert len(episodes) == 1
    assert len(episodes[0].events) == 12
    assert {item.group_id for item in episodes[0].events} == {"3001"}
    await repository.complete(episodes[0], proposals=0, committed=0)


@pytest.mark.asyncio
async def test_group_reflection_state_never_keeps_a_private_peer_and_repairs_legacy_rows(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="group-state-user",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="群聊消息",
        group_id="3001",
    )
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="group-state-yuki",
        scope_type=ScopeType.GROUP,
        sender_user_id="8000",
        direction="outbound",
        content="Yuki 的群聊回复",
        group_id="3001",
        sender_is_bot=True,
    )

    assert await repository.scan_new_events() == 2
    async with database.sessions() as session, session.begin():
        state = await session.scalar(select(MemorySelfReflectionStateModel))
        assert state is not None
        assert state.private_peer_user_id is None
        state.private_peer_user_id = "1001"

    assert await repository.scan_new_events() == 0
    async with database.sessions() as session:
        repaired = await session.scalar(select(MemorySelfReflectionStateModel))
    assert repaired is not None
    assert repaired.private_peer_user_id is None


@pytest.mark.asyncio
async def test_reflection_schedule_slot_and_daily_budget_are_idempotent(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    for group_number in range(4):
        group_id = str(4000 + group_number)
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"signal-{group_id}",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="你刚才说错了，这是一次重要纠正",
            group_id=group_id,
            occurred_at=datetime.now(UTC),
        )
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"reply-{group_id}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="我接受这次纠正",
            group_id=group_id,
            sender_is_bot=True,
        )
    await repository.scan_new_events()

    episodes = await repository.claim_due(
        scheduled_slot="2026-08-08:20",
        local_date="2026-08-08",
        event_threshold=2,
        character_threshold=6000,
        max_wait_seconds=28800,
        max_sessions=3,
        max_daily_calls=3,
        max_events=20,
        max_characters=8000,
    )

    assert len(episodes) == 3
    assert (
        await repository.claim_due(
            scheduled_slot="2026-08-08:20",
            local_date="2026-08-08",
            event_threshold=1,
            character_threshold=1,
            max_wait_seconds=1,
            max_sessions=3,
            max_daily_calls=3,
            max_events=20,
            max_characters=8000,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_manual_reflection_skips_schedule_and_volume_thresholds(
    database: Database,
) -> None:
    settings = Settings.model_validate(
        {
            "database_url": database.url,
            "memory_self_reflection_schedule_hours": "4,12,20",
        }
    )
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="manual-user-message",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="只有一条、远未达到自动触发阈值的消息",
        group_id="3001",
    )
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="manual-yuki-reply",
        scope_type=ScopeType.GROUP,
        sender_user_id="8000",
        direction="outbound",
        content="但这段对话确实有我的参与",
        group_id="3001",
        sender_is_bot=True,
    )
    service = RecordingSelfReflectionService()
    worker = SelfReflectionWorker(
        settings=settings,
        repository=repository,
        service=cast(SelfReflectionService, service),
        metrics=MemoryLifecycleMetrics(),
    )

    assert await worker.process_once(datetime(2026, 8, 10, 1, tzinfo=UTC)) == 0
    result = await worker.run_now()
    assert result.attempted_batches == 1
    assert result.completed_batches == 1
    assert result.failed_batches == 0
    assert len(service.batches) == 1
    assert service.batches[0].trigger_reason == "manual"
    assert len(service.batches[0].events) == 2


@pytest.mark.asyncio
async def test_cycle_can_process_multiple_non_overlapping_batches_from_one_conversation(
    database: Database,
) -> None:
    settings = Settings.model_validate(
        {
            "database_url": database.url,
            "memory_self_reflection_event_threshold": 2,
            "memory_self_reflection_low_event_threshold": 1,
            "memory_self_reflection_low_character_threshold": 1,
            "memory_self_reflection_max_events": 2,
            "memory_self_reflection_max_batches_per_run": 3,
            "memory_self_reflection_max_batches_per_conversation_per_run": 3,
            "memory_self_reflection_max_daily_calls": 10,
        }
    )
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    event_ids: list[int] = []
    for index in range(8):
        sender_is_bot = index % 2 == 1
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"multi-batch-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000" if sender_is_bot else "1001",
            direction="outbound" if sender_is_bot else "inbound",
            content=f"连续窗口消息 {index}",
            group_id="3001",
            sender_is_bot=sender_is_bot,
        )
        event_ids.append(event.id)
    service = RecordingSelfReflectionService()
    worker = SelfReflectionWorker(
        settings=settings,
        repository=repository,
        service=cast(SelfReflectionService, service),
        metrics=MemoryLifecycleMetrics(),
    )

    result = await worker.run_now()

    assert result.attempted_batches == 3
    assert result.completed_batches == 3
    assert [[event.id for event in batch.events] for batch in service.batches] == [
        event_ids[0:2],
        event_ids[2:4],
        event_ids[4:6],
    ]
    async with database.sessions() as session:
        state = await session.scalar(select(MemorySelfReflectionStateModel))
    assert state is not None
    assert state.last_event_id == event_ids[5]
    assert state.pending_events == 2


@pytest.mark.asyncio
async def test_failed_conversation_is_not_retried_in_the_same_cycle(database: Database) -> None:
    settings = Settings.model_validate(
        {
            "database_url": database.url,
            "memory_self_reflection_event_threshold": 2,
            "memory_self_reflection_low_event_threshold": 1,
            "memory_self_reflection_low_character_threshold": 1,
            "memory_self_reflection_max_events": 2,
            "memory_self_reflection_max_batches_per_run": 7,
            "memory_self_reflection_max_batches_per_conversation_per_run": 7,
            "memory_self_reflection_max_daily_calls": 10,
        }
    )
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    for index in range(4):
        sender_is_bot = index % 2 == 1
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"failed-multi-batch-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000" if sender_is_bot else "1001",
            direction="outbound" if sender_is_bot else "inbound",
            content=f"失败窗口消息 {index}",
            group_id="3001",
            sender_is_bot=sender_is_bot,
        )
    service = FailingSelfReflectionService()
    worker = SelfReflectionWorker(
        settings=settings,
        repository=repository,
        service=cast(SelfReflectionService, service),
        metrics=MemoryLifecycleMetrics(),
    )

    result = await worker.run_now()

    assert result.attempted_batches == 1
    assert result.failed_batches == 1
    assert service.calls == 1


@pytest.mark.asyncio
async def test_failed_reflection_batch_is_not_reported_as_completed(
    database: Database,
) -> None:
    settings = Settings.model_validate({"database_url": database.url})
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="failed-user-message",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="请回想这段群聊",
        group_id="3001",
    )
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="failed-yuki-reply",
        scope_type=ScopeType.GROUP,
        sender_user_id="8000",
        direction="outbound",
        content="这是我的回复",
        group_id="3001",
        sender_is_bot=True,
    )
    service = FailingSelfReflectionService()
    worker = SelfReflectionWorker(
        settings=settings,
        repository=repository,
        service=cast(SelfReflectionService, service),
        metrics=MemoryLifecycleMetrics(),
    )

    result = await worker.run_now()

    assert result.attempted_batches == 1
    assert result.completed_batches == 0
    assert result.failed_batches == 1
    assert result.proposal_count == 0
    assert result.committed_count == 0


@pytest.mark.asyncio
async def test_self_reflection_command_requires_superuser_and_reports_usage(
    database: Database,
) -> None:
    settings = Settings.model_validate(
        {
            "database_url": database.url,
            "superusers_csv": "9000",
        }
    )
    memories = MemoryFactService(MemoryFactRepository(database))
    audit = AdminAuditService(database)
    worker = RecordingSelfReflectionWorker()
    memory_admin = MemoryAdminService(
        settings=settings,
        memories=memories,
        audit=audit,
        self_reflection=cast(SelfReflectionWorker, worker),
    )
    handler = ProfileCommandHandler(
        people=cast(PeopleRepository, object()),
        memories=memories,
        memory_admin=memory_admin,
        preference_admin=cast(PreferenceAdminService, object()),
        relationship_admin=cast(RelationshipAdminService, object()),
    )
    admin = AdminActor(
        user_id="9000",
        is_superuser=True,
        trigger_message_id="admin-command",
        conversation_key="private:9000",
    )
    user = AdminActor(
        user_id="1001",
        is_superuser=False,
        trigger_message_id="user-command",
        conversation_key="private:1001",
    )

    result = await handler.memory(actor=admin, argument="self-reflection run")

    assert "尝试 1 个批次，成功 1 个，失败 0 个" in result
    assert "实际写入 0 条" in result
    assert "今日反思批次 1/36" in result
    assert worker.calls == 1
    worker.result = SelfReflectionCycleResult(
        attempted_batches=1,
        failed_batches=1,
    )
    failed_result = await handler.memory(actor=admin, argument="self-reflection run")
    assert "尝试 1 个批次，成功 0 个，失败 1 个" in failed_result
    assert "实际写入 0 条" in failed_result
    assert worker.calls == 2
    assert "只有超级管理员" in await handler.memory(
        actor=user,
        argument="self-reflection run",
    )
    assert worker.calls == 2


@pytest.mark.asyncio
async def test_tool_receipt_is_bounded_and_redacts_nested_json_secrets(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="tool-receipt-source",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="请查询状态",
        private_peer_user_id="1001",
    )
    repository = MCPRepository(database, reflection_excerpt_characters=120)
    await repository.record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="status",
        success=True,
        latency_seconds=0,
        result_size=200,
        artifact_created=False,
        error_category=None,
        trigger_message_id=event.platform_message_id,
        bot_user_id=event.bot_user_id,
        result_excerpt=json.dumps(
            {
                "data": {"token": "secret-value", "result": "ok"},
                "api-key": "another-secret",
            }
        ),
    )

    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryToolReceiptModel))
    assert receipt is not None
    assert "secret-value" not in receipt.result_excerpt
    assert "another-secret" not in receipt.result_excerpt
    assert receipt.result_excerpt.count("[redacted]") == 2
    assert len(receipt.result_excerpt) <= 120


@pytest.mark.asyncio
async def test_reflection_uses_oldest_window_context_and_keeps_concurrent_arrivals(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    historical, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="episode-context-before-baseline",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="这是上线前的一句前置上下文",
        group_id="3001",
    )
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    pending_ids: list[int] = []
    for index in range(12):
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"oldest-window-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000" if index == 2 else "1001",
            direction="outbound" if index == 2 else "inbound",
            content=f"上线后的第 {index + 1} 条连续消息",
            group_id="3001",
            sender_is_bot=index == 2,
        )
        pending_ids.append(event.id)
    assert await repository.scan_new_events() == 12

    first = (
        await repository.claim_due(
            scheduled_slot="2026-08-09:04",
            local_date="2026-08-09",
            event_threshold=1,
            character_threshold=8000,
            max_wait_seconds=28800,
            max_sessions=1,
            max_daily_calls=9,
            max_events=5,
            max_characters=8000,
            context_events=4,
        )
    )[0]
    assert [event.id for event in first.events] == pending_ids[:5]
    assert [event.id for event in first.context_events] == [historical.id]

    concurrent_ids: list[int] = []
    for index in range(2):
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"during-reflection-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000" if index == 1 else "1001",
            direction="outbound" if index == 1 else "inbound",
            content=f"模型运行期间新到的第 {index + 1} 条消息",
            group_id="3001",
            sender_is_bot=index == 1,
        )
        concurrent_ids.append(event.id)
    assert await repository.scan_new_events() == 2
    await repository.complete(first, proposals=0, committed=0)

    async with database.sessions() as session:
        state = await session.scalar(select(MemorySelfReflectionStateModel))
    assert state is not None
    assert state.last_event_id == pending_ids[4]
    assert state.pending_events == 9
    assert state.latest_event_id == concurrent_ids[-1]

    second = (
        await repository.claim_due(
            scheduled_slot="2026-08-09:12",
            local_date="2026-08-09",
            event_threshold=1,
            character_threshold=8000,
            max_wait_seconds=28800,
            max_sessions=1,
            max_daily_calls=9,
            max_events=5,
            max_characters=8000,
            context_events=4,
        )
    )[0]
    assert [event.id for event in second.events] == pending_ids[5:10]
    assert [event.id for event in second.context_events] == pending_ids[1:5]


@pytest.mark.asyncio
async def test_reflection_watermarks_cut_at_natural_pause_without_source_overlap(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    event_ids: list[int] = []
    occurred_at = datetime(2026, 8, 10, 1, tzinfo=UTC)
    for index in range(50):
        if index == 37:
            occurred_at += timedelta(minutes=10)
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"watermark-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000" if index in {5, 42} else "1001",
            direction="outbound" if index in {5, 42} else "inbound",
            content=f"连续对话中的第 {index + 1} 条消息",
            group_id="3001",
            sender_is_bot=index in {5, 42},
            occurred_at=occurred_at,
        )
        event_ids.append(event.id)
        occurred_at += timedelta(minutes=1)
    assert await repository.scan_new_events() == 50

    first = (
        await repository.claim_due(
            scheduled_slot="2026-08-10:04",
            local_date="2026-08-10",
            event_threshold=50,
            character_threshold=8000,
            low_event_threshold=30,
            low_character_threshold=4800,
            natural_gap_seconds=300,
            max_wait_seconds=28800,
            max_sessions=1,
            max_daily_calls=9,
            max_events=50,
            max_characters=8000,
            context_events=4,
        )
    )[0]
    assert [event.id for event in first.events] == event_ids[:37]
    assert first.context_events == ()
    await repository.complete(first, proposals=0, committed=0)

    second = (
        await repository.claim_due(
            scheduled_slot="manual:watermark-tail",
            local_date="2026-08-10",
            event_threshold=50,
            character_threshold=8000,
            low_event_threshold=30,
            low_character_threshold=4800,
            natural_gap_seconds=300,
            max_wait_seconds=28800,
            max_sessions=1,
            max_daily_calls=9,
            max_events=50,
            max_characters=8000,
            context_events=4,
            force=True,
        )
    )[0]
    assert [event.id for event in second.events] == event_ids[37:]
    assert [event.id for event in second.context_events] == event_ids[33:37]
    assert {event.id for event in first.events}.isdisjoint(event.id for event in second.events)


@pytest.mark.asyncio
async def test_reflection_uses_latest_episode_from_the_same_scope(
    database: Database,
) -> None:
    facts = MemoryFactService(MemoryFactRepository(database))

    async def remember_episode(*, group_id: str, key: str, content: str) -> None:
        await facts.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.SELF,
                visibility_type=SelfMemoryVisibility.GROUP,
                visibility_group_id=group_id,
                kind=MemoryKind.EPISODE,
                memory_key=key,
                category="self_episode",
                content=content,
                importance=4,
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.AGENT_REFLECTION,
            )
        )

    await remember_episode(group_id="3001", key="older", content="较早的同群经历")
    await remember_episode(group_id="3002", key="other", content="另一个群的经历")
    await remember_episode(group_id="3001", key="latest", content="最近的同群经历")
    service = object.__new__(SelfReflectionService)
    service._facts = facts
    batch = cast(
        SelfReflectionBatch,
        SimpleNamespace(
            state=SimpleNamespace(
                scope_type=ScopeType.GROUP,
                group_id="3001",
                private_peer_user_id=None,
            )
        ),
    )

    previous = await service._previous_episode(batch)

    assert previous is not None
    assert previous.content == "最近的同群经历"


@pytest.mark.asyncio
async def test_reflection_character_limit_keeps_the_oldest_event(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    first, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="oversized-oldest-event",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="长" * 9000,
        private_peer_user_id="1001",
    )
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="reply-after-oversized-event",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="我看到了",
        private_peer_user_id="1001",
        sender_is_bot=True,
    )
    await repository.scan_new_events()

    batch = (
        await repository.claim_due(
            scheduled_slot="2026-08-09:20",
            local_date="2026-08-09",
            event_threshold=1,
            character_threshold=1,
            max_wait_seconds=28800,
            max_sessions=1,
            max_daily_calls=9,
            max_events=20,
            max_characters=8000,
        )
    )[0]

    assert [event.id for event in batch.events] == [first.id]
    assert batch.max_input_characters == 8000


@pytest.mark.asyncio
async def test_reflection_batches_same_conversation_separately_per_bot(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    for bot_user_id in ("8000", "9000"):
        await ledger.append(
            bot_user_id=bot_user_id,
            platform_message_id=f"{bot_user_id}-user-message",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content=f"只属于机器人 {bot_user_id} 的输入",
            group_id="3001",
        )
        await ledger.append(
            bot_user_id=bot_user_id,
            platform_message_id=f"{bot_user_id}-bot-reply",
            scope_type=ScopeType.GROUP,
            sender_user_id=bot_user_id,
            direction="outbound",
            content=f"机器人 {bot_user_id} 的真实回复",
            group_id="3001",
            sender_is_bot=True,
        )
    await repository.scan_new_events()

    batches = await repository.claim_due(
        scheduled_slot="2026-08-10:04",
        local_date="2026-08-10",
        event_threshold=1,
        character_threshold=8000,
        max_wait_seconds=28800,
        max_sessions=3,
        max_daily_calls=9,
        max_events=20,
        max_characters=8000,
    )

    assert len(batches) == 2
    assert {batch.state.bot_user_id for batch in batches} == {"8000", "9000"}
    assert all(
        {event.bot_user_id for event in batch.events} == {batch.state.bot_user_id}
        for batch in batches
    )


def test_reflection_output_allows_zero_or_one_free_episode() -> None:
    assert SelfReflectionOutput.model_validate({}).episodes == ()
    one = SelfReflectionOutput.model_validate(
        {
            "episodes": [
                {
                    "content": "我记得那天终于把问题修好了",
                    "importance": 4,
                    "evidence_refs": ["event_1"],
                }
            ]
        }
    )
    assert one.episodes[0].content == "我记得那天终于把问题修好了"
    with pytest.raises(ValidationError, match="at most one episode"):
        SelfReflectionOutput.model_validate(
            {
                "episodes": [
                    {
                        "content": "QQ 2186567848 当时说终于成功了",
                        "importance": 4,
                        "evidence_refs": ["event_1"],
                    },
                    {
                        "content": "在群 1049765710 里，我后来觉得这件事挺有趣",
                        "importance": 3,
                        "evidence_refs": ["event_2"],
                    },
                ]
            }
        )


def test_reflection_output_keeps_episode_out_of_fact_proposals() -> None:
    schema = SelfReflectionOutput.model_json_schema()
    proposal_kind = schema["$defs"]["SelfReflectionProposal"]["properties"]["kind"]
    encoded_kind_schema = json.dumps(proposal_kind, ensure_ascii=False)
    proposal_category = schema["$defs"]["SelfReflectionProposal"]["properties"]["category"]
    encoded_category_schema = json.dumps(proposal_category, ensure_ascii=False)

    assert '"fact"' in encoded_kind_schema
    assert '"preference"' in encoded_kind_schema
    assert '"episode"' not in encoded_kind_schema
    assert '"self_fact"' in encoded_category_schema
    assert '"self_preference"' in encoded_category_schema
    assert '"self_reflection"' in encoded_category_schema
    assert '"self_principle"' in encoded_category_schema
    assert '"self_episode"' not in encoded_category_schema
    with pytest.raises(ValidationError):
        SelfReflectionOutput.model_validate(
            {
                "proposals": [
                    {
                        "operation": "create",
                        "evidence_refs": ["event_1"],
                        "category": "self_episode",
                        "kind": "episode",
                        "memory_key": "self_episode:wrong-channel",
                        "content": "这条经历不应该出现在 proposals",
                        "reason": "wrong output channel",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        SelfReflectionOutput.model_validate(
            {
                "proposals": [
                    {
                        "operation": "create",
                        "evidence_refs": ["event_1"],
                        "category": "self_episode",
                        "kind": "fact",
                        "memory_key": "self_episode:disguised-channel",
                        "content": "也不能把 Episode 伪装成普通 fact",
                        "reason": "wrong output category",
                    }
                ]
            }
        )


def test_candidate_decision_matches_the_proposal_operation() -> None:
    with pytest.raises(ValidationError, match="acceptance requires a memory mutation"):
        SelfReflectionOutput.model_validate(
            {
                "proposals": [
                    {
                        "operation": "noop",
                        "candidate_ref": "candidate_1",
                        "candidate_decision": "accept",
                        "reason": "不能只说接受却不写入",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError, match="rejection or deferral requires noop"):
        SelfReflectionOutput.model_validate(
            {
                "proposals": [
                    {
                        "operation": "create",
                        "candidate_ref": "candidate_1",
                        "candidate_decision": "reject",
                        "evidence_refs": ["event_1"],
                        "category": "self_fact",
                        "kind": "fact",
                        "memory_key": "test:candidate",
                        "content": "候选内容",
                        "reason": "不能一边拒绝一边写入",
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_all_rejected_mutation_proposals_fail_the_batch() -> None:
    output = SelfReflectionOutput.model_validate(
        {
            "proposals": [
                {
                    "operation": "create",
                    "evidence_refs": ["event_1"],
                    "category": "self_fact",
                    "kind": "fact",
                    "memory_key": "test:rejected",
                    "content": "这条提案会被后端拒绝",
                    "reason": "测试游标不能误推进",
                }
            ]
        }
    )
    service = object.__new__(SelfReflectionService)

    async def fake_input(_batch: object) -> tuple[object, dict, dict, dict, dict]:
        return object(), {}, {}, {}, {}

    async def reject_apply(*_args: object, **_kwargs: object) -> bool:
        return False

    service._input = fake_input  # type: ignore[method-assign]
    service._apply = reject_apply  # type: ignore[method-assign]
    service._concurrency = StaticReflectionConcurrency(output)  # type: ignore[assignment]
    service._metrics = MemoryLifecycleMetrics()

    with pytest.raises(RuntimeError, match="all self-reflection mutations failed"):
        await service.reflect(cast(SelfReflectionBatch, object()))
