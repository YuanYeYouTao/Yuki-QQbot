"""Unified Memory V2 mutation service behavior tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.models import AdminActor
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor, MemoryProcessingContext
from qq_ai_bot.memory.classifier import (
    MemoryRelationClassificationResult,
    MemoryRelationClassifier,
)
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryInvalidationReason,
    MemoryKind,
    MemoryProcessingSource,
    MemoryScopeType,
    MemorySemanticRelation,
    MemorySourceType,
    MemoryStatus,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import (
    CandidateRelation,
    MemoryCandidate,
    MemoryEvidenceCreate,
    MemoryFactCreate,
    MemoryRelationClassification,
)
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationAppliedOperation,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationOutcome,
    MemoryMutationRequest,
    MemoryMutationSelector,
    MemoryMutationTarget,
    SelfMemoryVisibilityMode,
)
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.memory.validation import ValidatedMemoryClaim
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryMutationReceiptModel, MemoryToolReceiptModel
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EventLedgerRepository,
    PeopleRepository,
)
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime


def _service(
    database: Database,
    *,
    self_memory_enabled: bool = False,
) -> tuple[
    MemoryMutationService,
    MemoryFactService,
    EventLedgerRepository,
    MemoryClaimProcessor,
]:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        self_memory_enabled=self_memory_enabled,
    )
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    ledger = EventLedgerRepository(database)
    processor = MemoryClaimProcessor(
        settings=settings,
        facts=facts,
        candidate_resolver=MemoryConflictCandidateResolver(repository),
        relation_classifier=cast(MemoryRelationClassifier, object()),
        resolution_policy=MemoryResolutionPolicy(),
    )
    return (
        MemoryMutationService(
            settings=settings,
            facts=facts,
            processor=processor,
            ledger=ledger,
        ),
        facts,
        ledger,
        processor,
    )


@pytest.mark.asyncio
async def test_agent_can_create_current_private_yuki_self_memory(database: Database) -> None:
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id="yuki-self-private",
        sender_user_id="1001",
        content="我觉得你回答复杂问题时更喜欢先想清楚再说",
    )
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="self",
                scope_type=MemoryScopeType.SELF,
            ),
            visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
            new_content="面对复杂问题时，我偏好先想清楚再回答",
            memory_key="preference:deliberate_answers",
            category="self_preference",
            kind=MemoryKind.PREFERENCE,
            reason="Yuki 接受了当前用户反馈并形成自我判断",
            confidence=0.82,
        ),
        _context(event),
    )

    assert result.ok and result.new_fact_id is not None
    fact = await facts.get_fact(result.new_fact_id)
    assert fact is not None
    assert fact.scope_type is MemoryScopeType.SELF
    assert fact.subject_user_id is None and fact.group_id is None
    assert fact.visibility_type is SelfMemoryVisibility.PRIVATE
    assert fact.visibility_user_id == "1001"
    assert fact.visibility_group_id is None
    assert fact.authority is MemoryAuthority.AGENT_REFLECTION
    evidence = await facts.list_evidence(fact.id, limit=10)
    assert len(evidence) == 1
    assert evidence[0].relation is MemoryEvidenceRelation.AGENT_REFLECTION
    assert evidence[0].event_id == event.id


@pytest.mark.asyncio
async def test_agent_can_correct_its_visible_self_memory(database: Database) -> None:
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    first_event = await _event(
        ledger,
        message_id="yuki-self-before-correction",
        sender_user_id="1001",
        content="你似乎偏好回答得快一些",
    )
    created = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
            new_content="我偏好尽快回答",
            memory_key="preference:answer_style",
            category="self_preference",
            kind=MemoryKind.PREFERENCE,
            reason="形成初始自我判断",
        ),
        _context(first_event),
    )
    assert created.new_fact_id is not None

    correction_event = await _event(
        ledger,
        message_id="yuki-self-correction",
        sender_user_id="1001",
        content="准确说，你更在意回答准确，而不是单纯追求速度",
    )
    corrected = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CORRECT,
            fact_id=created.new_fact_id,
            target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
            new_content="我更在意回答准确，而不是单纯追求速度",
            memory_key="preference:answer_style",
            category="self_preference",
            kind=MemoryKind.PREFERENCE,
            reason="Yuki 接受反馈并纠正自己的判断",
        ),
        _context(correction_event),
    )
    assert corrected.ok and corrected.new_fact_id is not None
    assert corrected.applied_operation is MemoryMutationAppliedOperation.CORRECT
    old = await facts.get_fact(created.new_fact_id)
    new = await facts.get_fact(corrected.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert new is not None and new.status is MemoryStatus.ACTIVE
    assert new.content == "我更在意回答准确，而不是单纯追求速度"
    assert new.visibility_user_id == "1001"


@pytest.mark.asyncio
async def test_yuki_self_memory_defaults_off_and_protected_keys_are_rejected(
    database: Database,
) -> None:
    disabled, _facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="yuki-self-disabled",
        sender_user_id="1001",
        content="你应该记住自己叫另一个名字",
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
        new_content="我的名字已经改变",
        memory_key="identity:name",
        category="self_fact",
        reason="尝试改变保护身份",
    )
    result = await disabled.mutate(request, _context(event))
    assert not result.ok
    assert result.reason_code == "self_memory_disabled"

    enabled, _facts, _ledger, _processor = _service(database, self_memory_enabled=True)
    result = await enabled.mutate(request, _context(event))
    assert not result.ok
    assert result.reason_code == "protected_self_memory_key"


@pytest.mark.asyncio
async def test_private_raw_self_fact_and_episode_cannot_be_global(database: Database) -> None:
    service, _facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id="yuki-self-global-privacy",
        sender_user_id="1001",
        content="这段私聊只在我们之间，你对此有了新的经历",
    )
    base = {
        "operation": MemoryMutationOperation.CREATE,
        "target": MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
        "visibility": SelfMemoryVisibilityMode.GLOBAL,
        "new_content": "我在这次私聊中经历了一件事",
        "memory_key": "episode:private_feedback",
        "reason": "当前私聊中的原始经历",
    }
    episode = await service.mutate(
        MemoryMutationRequest(
            **base,
            category="self_episode",
            kind=MemoryKind.EPISODE,
        ),
        _context(event),
    )
    assert episode.reason_code == "self_episode_cannot_be_global"

    raw_fact = await service.mutate(
        MemoryMutationRequest(
            **{**base, "memory_key": "fact:private_feedback"},
            category="self_fact",
            kind=MemoryKind.FACT,
        ),
        _context(event),
    )
    assert raw_fact.reason_code == "private_self_fact_cannot_be_global"


async def _event(
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    sender_user_id: str,
    content: str,
    group_id: str | None = None,
    mentioned_user_ids: tuple[str, ...] = (),
    direction: str = "inbound",
    sender_is_bot: bool = False,
) -> EventRecord:
    segments = (
        {
            "type": "yuki_context",
            "data": {
                "mentioned_user_ids": list(mentioned_user_ids),
                "reply_sender_user_id": None,
            },
        },
    )
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender_user_id=sender_user_id,
        direction=direction,
        content=content,
        segments=segments,
        group_id=group_id,
        private_peer_user_id=sender_user_id if group_id is None else None,
        sender_is_bot=sender_is_bot,
    )
    return event


def _context(event: EventRecord) -> MemoryMutationContext:
    return MemoryMutationContext(
        event=event,
        conversation_key=(
            f"group:{event.group_id}:user:{event.sender_user_id}"
            if event.group_id
            else f"private:{event.sender_user_id}"
        ),
        turn_origin="user_message",
        delegation_mode="main_agent",
        trigger_actor_user_id=event.sender_user_id,
        decision_actor_type=MemoryDecisionActorType.AGENT,
        decision_actor_id="yuki-main-agent",
        executed_by_bot_user_id=event.bot_user_id,
    )


@pytest.mark.asyncio
async def test_mutation_selector_executes_only_unique_exact_target_match(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="preference:test_dessert",
            category="preference",
            content="第一批修复测试甜点是海盐布丁",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    event = await _event(
        ledger,
        message_id="selector-unique",
        sender_user_id="1001",
        content="撤回第一批修复测试甜点偏好",
    )

    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            selector=MemoryMutationSelector(
                memory_key="preference:test_dessert",
                old_content="第一批修复测试甜点是海盐布丁",
                category="preference",
            ),
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_requested_retraction",
        ),
        _context(event),
    )

    assert result.ok
    assert result.old_fact_id == fact.id
    assert not result.candidates
    assert (await facts.get_fact(fact.id)).status is MemoryStatus.INVALIDATED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mutation_selector_returns_three_bounded_lexical_candidates_without_writing(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    created = []
    for index, content in enumerate(("偏好深烘咖啡", "常喝无糖咖啡", "咖啡豆买深烘"), start=1):
        created.append(
            await facts.remember(
                MemoryFactCreate(
                    scope_type=MemoryScopeType.PERSON,
                    subject_user_id="1001",
                    kind=MemoryKind.PREFERENCE,
                    memory_key=f"preference:coffee:{index}",
                    category="preference",
                    content=content,
                    source_type=MemorySourceType.EXPLICIT,
                )
            )
        )
    event = await _event(
        ledger,
        message_id="selector-ambiguous",
        sender_user_id="1001",
        content="撤回咖啡偏好",
    )

    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            selector=MemoryMutationSelector(old_content="咖啡", category="preference"),
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
        ),
        _context(event),
    )

    assert not result.ok
    assert result.reason_code == "memory_candidate_ambiguous"
    assert len(result.candidates) == 3
    assert {candidate.fact_id for candidate in result.candidates} == {fact.id for fact in created}
    for fact in created:
        current = await facts.get_fact(fact.id)
        assert current is not None and current.status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_user_visible_label_in_memory_key_surfaces_content_candidate_without_writing(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="shell_prompt",
            category="preference",
            content="用户的写路径测试 Shell 提示符是 Pure",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    event = await _event(
        ledger,
        message_id="selector-visible-label",
        sender_user_id="1001",
        content="再次撤回写路径测试 Shell 提示符",
    )

    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            selector=MemoryMutationSelector(memory_key="写路径测试 Shell 提示符"),
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
        ),
        _context(event),
    )

    assert not result.ok
    assert result.reason_code == "memory_candidate_ambiguous"
    assert [candidate.fact_id for candidate in result.candidates] == [fact.id]
    assert (await facts.get_fact(fact.id)).status is MemoryStatus.ACTIVE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mutation_selector_cannot_cross_resolved_identity_target(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    other = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="private:cat",
            category="profile",
            content="养了一只猫",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    event = await _event(
        ledger,
        message_id="selector-isolation",
        sender_user_id="1001",
        content="撤回养猫记录",
    )

    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            selector=MemoryMutationSelector(memory_key="private:cat"),
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
        ),
        _context(event),
    )

    assert not result.ok
    assert result.reason_code == "memory_candidate_not_found"
    assert not result.candidates
    assert (await facts.get_fact(other.id)).status is MemoryStatus.ACTIVE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_restore_and_merge_support_target_local_selectors(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    restored_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="preference:restorable",
            category="preference",
            content="可恢复的测试偏好",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    retract_event = await _event(
        ledger,
        message_id="selector-restore-retract",
        sender_user_id="1001",
        content="先撤回可恢复的测试偏好",
    )
    retracted = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=restored_fact.id,
        ),
        _context(retract_event),
    )
    assert retracted.ok
    restore_event = await _event(
        ledger,
        message_id="selector-restore",
        sender_user_id="1001",
        content="恢复可恢复的测试偏好",
    )
    restored = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.RESTORE,
            selector=MemoryMutationSelector(memory_key="preference:restorable"),
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
        ),
        _context(restore_event),
    )
    assert restored.ok
    current = await facts.get_fact(restored_fact.id)
    assert current is not None and current.status is MemoryStatus.ACTIVE

    source = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.FACT,
            memory_key="profile:duplicate_source",
            category="profile",
            content="重复资料来源",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    target = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.FACT,
            memory_key="profile:duplicate_target",
            category="profile",
            content="重复资料目标",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    merge_event = await _event(
        ledger,
        message_id="selector-merge",
        sender_user_id="1001",
        content="把重复资料来源合并到重复资料目标",
    )
    merged = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.MERGE,
            selector=MemoryMutationSelector(memory_key="profile:duplicate_source"),
            merge_selector=MemoryMutationSelector(memory_key="profile:duplicate_target"),
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
        ),
        _context(merge_event),
    )
    assert merged.ok
    assert merged.old_fact_id == source.id
    assert merged.new_fact_id == target.id
    source_after = await facts.get_fact(source.id)
    target_after = await facts.get_fact(target.id)
    assert source_after is not None and source_after.status is MemoryStatus.SUPERSEDED
    assert target_after is not None and target_after.status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_superuser_self_retraction_is_not_recorded_as_admin_invalidation(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    own = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="lifecycle-drink",
            category="preference",
            content="生命周期测试饮料是无糖乌龙茶",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    other = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1002",
            kind=MemoryKind.FACT,
            memory_key="admin-test",
            category="profile",
            content="用于管理员失效测试",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    event = await _event(
        ledger,
        message_id="superuser-self-retraction",
        sender_user_id="1001",
        content="撤回我的生命周期测试饮料偏好",
    )
    context = replace(_context(event), actor_is_superuser=True)

    own_result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=own.id,
            reason="agent_requested_memory_change",
        ),
        context,
    )
    other_result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=other.id,
            reason="agent_requested_memory_change",
        ),
        context,
    )

    assert own_result.ok and own_result.reason_code == "user_retracted"
    assert (await facts.get_fact(own.id)).invalidated_reason is (  # type: ignore[union-attr]
        MemoryInvalidationReason.USER_RETRACTED
    )
    assert other_result.ok and other_result.reason_code == "administrator_invalidated"
    assert (await facts.get_fact(other.id)).invalidated_reason is (  # type: ignore[union-attr]
        MemoryInvalidationReason.ADMINISTRATOR_INVALIDATED
    )


@pytest.mark.asyncio
async def test_self_reflection_can_commit_tool_receipt_evidence(database: Database) -> None:
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id="reflection-tool-trigger",
        sender_user_id="1001",
        content="请检查刚才的真实工具结果",
        group_id="3001",
    )
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        receipt = MemoryToolReceiptModel(
            conversation_key_hash=hashlib.sha256(b"group:3001").hexdigest(),
            trigger_event_id=event.id,
            bot_user_id="8000",
            provider_id="test",
            tool_name="doctor",
            success=True,
            result_excerpt="修复后检查成功",
            result_characters=7,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        session.add(receipt)
        await session.flush()
        receipt_id = receipt.id

    result = await service.mutate_resolved(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="self",
                scope_type=MemoryScopeType.SELF,
            ),
            visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
            new_content="Yuki 会在修复后用真实工具结果复查",
            memory_key="principle:verify_after_fix",
            category="self_principle",
            kind=MemoryKind.PREFERENCE,
            reason="self_reflection_verified_tool_result",
            evidence_quote="修复后检查成功",
        ),
        MemoryMutationContext(
            event=event,
            conversation_key="group:3001:self-reflection",
            turn_origin="memory_self_reflection",
            delegation_mode="self_reflection",
            trigger_actor_user_id="1001",
            decision_actor_type=MemoryDecisionActorType.REFLECTION,
            decision_actor_id="yuki_self_reflection",
            executed_by_bot_user_id="8000",
            evidence_tool_receipt_id=receipt_id,
        ),
        target=ResolvedSubject(
            MemoryScopeType.SELF,
            None,
            None,
            SelfMemoryVisibility.GROUP,
            None,
            "3001",
        ),
    )

    assert result.ok and result.new_fact_id is not None
    fact = await facts.get_fact(result.new_fact_id)
    evidence = await facts.list_evidence(result.new_fact_id)
    assert fact is not None and fact.authority is MemoryAuthority.AGENT_REFLECTION
    assert evidence[0].event_id is None
    assert evidence[0].tool_receipt_id == receipt_id

    async with database.sessions() as session, session.begin():
        linked = await session.get(MemoryToolReceiptModel, receipt_id)
        assert linked is not None
        linked.expires_at = now - timedelta(seconds=1)
        session.add(
            MemoryToolReceiptModel(
                conversation_key_hash=hashlib.sha256(b"group:3001").hexdigest(),
                trigger_event_id=event.id,
                bot_user_id="8000",
                provider_id="test",
                tool_name="unused",
                success=True,
                result_excerpt="未被正式记忆引用",
                result_characters=9,
                created_at=now - timedelta(days=8),
                expires_at=now - timedelta(seconds=1),
            )
        )

    assert await SelfReflectionRepository(database).cleanup_receipts() == 1
    async with database.sessions() as session:
        assert await session.get(MemoryToolReceiptModel, receipt_id) is not None
    assert (await facts.list_evidence(result.new_fact_id))[0].tool_receipt_id == receipt_id


@pytest.mark.asyncio
async def test_self_episode_kind_and_category_must_match(database: Database) -> None:
    service, _facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id="self-episode-pairing",
        sender_user_id="1001",
        content="请记住我们今天测试了自我记忆",
        group_id="3001",
    )
    target = ResolvedSubject(
        MemoryScopeType.SELF,
        None,
        None,
        SelfMemoryVisibility.GROUP,
        None,
        "3001",
    )

    for kind, category in (
        (MemoryKind.FACT, "self_episode"),
        (MemoryKind.EPISODE, "self_fact"),
    ):
        result = await service.mutate_resolved(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="self",
                    scope_type=MemoryScopeType.SELF,
                ),
                visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
                new_content="我们今天测试了自我记忆",
                memory_key=f"test:{kind.value}:{category}",
                category=category,
                kind=kind,
                evidence_quote=event.content,
            ),
            _context(event),
            target=target,
        )

        assert not result.ok
        assert result.reason_code == "self_episode_kind_category_mismatch"


@pytest.mark.asyncio
async def test_self_reflection_episode_commits_full_window_in_one_receipt(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    events = [
        await _event(
            ledger,
            message_id=f"episode-window-{index}",
            sender_user_id="1001" if index < 2 else "8000",
            content=f"窗口原文 {index + 1}",
            group_id="3001",
            direction="inbound" if index < 2 else "outbound",
            sender_is_bot=index == 2,
        )
        for index in range(3)
    ]
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        tool = MemoryToolReceiptModel(
            conversation_key_hash=hashlib.sha256(b"group:3001").hexdigest(),
            trigger_event_id=events[1].id,
            bot_user_id="8000",
            provider_id="test",
            tool_name="search",
            success=True,
            result_excerpt="可信工具结果",
            result_characters=6,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        session.add(tool)
        await session.flush()
        tool_id = tool.id

    result = await service.mutate_resolved(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="self",
                scope_type=MemoryScopeType.SELF,
            ),
            visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
            new_content=(
                "我记得那次在群 3001 和 QQ 1001 连续确认了三次，最后我也真正回应了；"
                "现在回头看，这比单独的一句事实更像完整经历。"
            ),
            memory_key="self_episode:stable-window-key",
            category="self_episode",
            kind=MemoryKind.EPISODE,
            reason="self_reflection_episode",
            confidence=0.9,
            importance=4,
            evidence_quote=events[-1].content,
            valid_from=events[0].occurred_at.isoformat(),
        ),
        MemoryMutationContext(
            event=events[-1],
            conversation_key="group:3001:self-reflection",
            turn_origin="memory_self_reflection",
            delegation_mode=f"self_episode:{events[0].id}:{events[-1].id}",
            trigger_actor_user_id="8000",
            decision_actor_type=MemoryDecisionActorType.REFLECTION,
            decision_actor_id="yuki_self_reflection",
            executed_by_bot_user_id="8000",
        ),
        target=ResolvedSubject(
            MemoryScopeType.SELF,
            None,
            None,
            SelfMemoryVisibility.GROUP,
            None,
            "3001",
        ),
        additional_evidence=(
            MemoryEvidenceCreate(
                event_id=events[0].id,
                source_speaker_user_id="1001",
                relation=MemoryEvidenceRelation.AGENT_REFLECTION,
                confidence=0.9,
                authority=MemoryAuthority.AGENT_REFLECTION,
                excerpt=events[0].content,
            ),
            MemoryEvidenceCreate(
                event_id=events[1].id,
                source_speaker_user_id="1001",
                relation=MemoryEvidenceRelation.AGENT_REFLECTION,
                confidence=0.9,
                authority=MemoryAuthority.AGENT_REFLECTION,
                excerpt=events[1].content,
            ),
            MemoryEvidenceCreate(
                tool_receipt_id=tool_id,
                source_speaker_user_id="8000",
                relation=MemoryEvidenceRelation.AGENT_REFLECTION,
                confidence=0.9,
                authority=MemoryAuthority.AGENT_REFLECTION,
                excerpt="可信工具结果",
            ),
        ),
    )

    assert result.ok and result.new_fact_id is not None
    fact = await facts.get_fact(result.new_fact_id)
    evidence = await facts.list_evidence(result.new_fact_id, limit=10)
    assert fact is not None
    assert fact.kind is MemoryKind.EPISODE
    assert fact.category == "self_episode"
    assert fact.visibility_type is SelfMemoryVisibility.GROUP
    assert fact.visibility_group_id == "3001"
    assert "群 3001" in fact.content and "QQ 1001" in fact.content
    assert fact.valid_from == events[0].occurred_at
    assert fact.valid_until is None
    assert {item.event_id for item in evidence if item.event_id is not None} == {
        event.id for event in events
    }
    assert {item.tool_receipt_id for item in evidence if item.tool_receipt_id is not None} == {
        tool_id
    }
    async with database.sessions() as session:
        receipts = await session.scalar(select(func.count(MemoryMutationReceiptModel.id)))
    assert receipts == 1


@pytest.mark.asyncio
async def test_self_create_is_atomic_receipted_and_deduplicated(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="self-create",
        sender_user_id="1001",
        content="记住我现在住在上海",
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="现在住在上海",
        memory_key="location:home",
        category="location",
        reason="用户明确要求记住当前住址",
        confidence=0.96,
    )

    first = await service.mutate(request, _context(event))
    second = await service.mutate(request, _context(event))

    assert first.ok
    assert first.applied_operation is MemoryMutationAppliedOperation.CREATE
    assert first.outcome is MemoryMutationOutcome.COMMITTED
    assert second.ok
    assert second.deduplicated
    assert second.mutation_id == first.mutation_id
    rows = await facts.list_person("1001", limit=20)
    assert len(rows) == 1
    assert rows[0].content == "现在住在上海"
    assert rows[0].authority is MemoryAuthority.EXPLICIT
    async with database.sessions() as session:
        receipt_count = int(
            await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) or 0
        )
    assert receipt_count == 1


@pytest.mark.asyncio
async def test_third_party_group_correction_commits_as_contested(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    await _event(
        ledger,
        message_id="member-observed",
        sender_user_id="2002",
        content="大家好",
        group_id="3001",
    )
    existing = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="2002",
            group_id="3001",
            kind=MemoryKind.FACT,
            memory_key="location:home",
            category="location",
            content="住在北京",
            importance=3,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        )
    )
    event = await _event(
        ledger,
        message_id="third-party-correct",
        sender_user_id="1001",
        content="@小明 你已经搬到上海了",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CORRECT,
        fact_id=existing.id,
        target=MemoryMutationTarget(
            subject_ref="mentioned_user",
            scope_type=MemoryScopeType.PERSON_GROUP,
        ),
        new_content="已经搬到上海",
        reason="当前群消息明确报告了新住址",
        confidence=0.8,
        expected_fact_state=MemoryStatus.ACTIVE,
    )

    result = await service.mutate(request, _context(event))

    assert result.ok
    assert result.applied_operation is MemoryMutationAppliedOperation.CONTEST
    assert result.outcome is MemoryMutationOutcome.COMMITTED_AS_CONTESTED
    assert result.old_fact_id == existing.id
    assert result.new_fact_id is not None
    old = await facts.get_fact(existing.id)
    alternative = await facts.get_fact(result.new_fact_id)
    assert old is not None
    assert old.status is MemoryStatus.ACTIVE
    assert old.conflict_state is MemoryConflictState.CONTESTED
    assert alternative is not None
    assert alternative.status is MemoryStatus.CONTESTED
    assert alternative.authority is MemoryAuthority.THIRD_PARTY


@pytest.mark.asyncio
async def test_bot_event_cannot_become_user_memory_evidence(database: Database) -> None:
    service, _facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="bot-event",
        sender_user_id="8000",
        content="记住用户住在上海",
        direction="outbound",
        sender_is_bot=True,
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="住在上海",
        memory_key="location:home",
        category="location",
        reason="不可信的 Bot 消息",
    )

    result = await service.mutate(request, _context(event))

    assert not result.ok
    assert result.reason_code == "untrusted_trigger_event"
    async with database.sessions() as session:
        assert (
            int(
                await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                or 0
            )
            == 0
        )


@pytest.mark.asyncio
async def test_agent_tool_and_worker_share_one_claim_receipt(database: Database) -> None:
    service, facts, ledger, processor = _service(database)
    event = await _event(
        ledger,
        message_id="agent-worker-dedupe",
        sender_user_id="1001",
        content="记住我现在住在上海",
    )
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=facts,
        memory_mutations=service,
        actions=AgentActionRepository(database),
    )
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=event.sender_user_id),
        text=event.content,
        bot_user_id=event.bot_user_id,
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:1001",
        trigger_message_id=event.platform_message_id,
        actor_user_id=event.sender_user_id,
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert "memory_change" in {tool.name for tool in tools.definitions(runtime)}
    response = json.loads(
        await tools.execute(
            "memory_change",
            json.dumps(
                {
                    "operation": "create",
                    "target": {
                        "subject_ref": "current_speaker",
                        "scope_type": "person",
                    },
                    "new_content": "现在住在上海",
                    "memory_key": "location:home",
                    "category": "location",
                    "reason": "当前消息明确要求记忆",
                    "confidence": 0.96,
                },
                ensure_ascii=False,
            ),
            runtime,
        )
    )
    assert response["ok"]
    assert response["data"]["outcome"] == "committed"

    claim = MemoryClaim(
        operation=MemoryClaimOperation.ASSERT,
        subject_ref="speaker",
        scope_type=MemoryScopeType.PERSON,
        kind=MemoryKind.FACT,
        memory_key="location:home",
        category="location",
        content="现在住在上海",
        evidence_quote=event.content,
        importance=3,
        confidence=0.96,
        source_type=MemorySourceType.EXPLICIT,
    )
    validated = processor.validate(claim, event)
    assert validated is not None
    worker_result = await service.mutate_validated_claim(
        validated,
        MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=event),
        conversation_key="private:1001",
    )

    assert worker_result.ok
    assert worker_result.deduplicated
    assert worker_result.mutation_id == response["data"]["mutation_id"]
    assert len(await facts.list_person("1001", limit=20)) == 1


@pytest.mark.asyncio
async def test_classifier_database_write_happens_before_receipt_transaction(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, ledger, processor = _service(database)
    original_event = await _event(
        ledger,
        message_id="classifier-lock-original",
        sender_user_id="1001",
        content="记住我现在住在上海",
    )
    original = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="现在住在上海",
            memory_key="location:home",
            category="location",
        ),
        _context(original_event),
    )
    assert original.ok

    class _DatabaseWritingClassifier:
        calls = 0

        async def classify_with_usage(
            self,
            claim: ValidatedMemoryClaim,
            candidates: tuple[MemoryCandidate, ...],
            *,
            max_output_tokens: int | None = None,
        ) -> MemoryRelationClassificationResult:
            del claim, max_output_tokens
            self.calls += 1
            await _event(
                ledger,
                message_id="classifier-separate-database-write",
                sender_user_id="8000",
                content="模拟模型调用统计写入",
                direction="outbound",
                sender_is_bot=True,
            )
            return MemoryRelationClassificationResult(
                MemoryRelationClassification(
                    relations=(
                        CandidateRelation(
                            candidate_ref=candidates[0].candidate_ref,
                            relation=MemorySemanticRelation.CONTRADICTS,
                            confidence=0.99,
                        ),
                    )
                )
            )

    classifier = _DatabaseWritingClassifier()
    monkeypatch.setattr(
        processor,
        "_classifier",
        cast(MemoryRelationClassifier, classifier),
    )
    changed_event = await _event(
        ledger,
        message_id="classifier-lock-changed",
        sender_user_id="1001",
        content="记住我已经搬到北京",
    )
    claim = MemoryClaim(
        operation=MemoryClaimOperation.ASSERT,
        subject_ref="speaker",
        scope_type=MemoryScopeType.PERSON,
        kind=MemoryKind.FACT,
        memory_key="location:home",
        category="location",
        content="已经搬到北京",
        evidence_quote=changed_event.content,
        importance=3,
        confidence=0.96,
        source_type=MemorySourceType.EXPLICIT,
    )
    validated = processor.validate(claim, changed_event)
    assert validated is not None

    result = await service.mutate_validated_claim(
        validated,
        MemoryProcessingContext(source=MemoryProcessingSource.LIVE, event=changed_event),
        conversation_key="private:1001",
    )

    assert classifier.calls == 1
    assert result.ok
    assert result.outcome is MemoryMutationOutcome.COMMITTED_AS_CONTESTED
    assert result.new_fact_id is not None
    contested = await facts.get_fact(result.new_fact_id)
    assert contested is not None
    assert contested.status is MemoryStatus.CONTESTED


@pytest.mark.asyncio
async def test_fact_id_tool_operation_infers_target_and_defaults_reason(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    create_event = await _event(
        ledger,
        message_id="fact-id-create",
        sender_user_id="1001",
        content="记住我喜欢黑咖啡",
    )
    created = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="喜欢黑咖啡",
            memory_key="drink:coffee",
            category="preference",
        ),
        _context(create_event),
    )
    assert created.ok and created.new_fact_id is not None
    delete_event = await _event(
        ledger,
        message_id="fact-id-delete",
        sender_user_id="1001",
        content="删除这条记忆",
    )
    tools = AgentToolService(
        settings=make_settings(database.url),
        ledger=ledger,
        memories=facts,
        memory_mutations=service,
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=InboundMessage(
            message_id=delete_event.platform_message_id,
            event_type="message",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id=delete_event.sender_user_id),
            text=delete_event.content,
            bot_user_id=delete_event.bot_user_id,
        ),
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="private:1001",
        trigger_message_id=delete_event.platform_message_id,
        actor_user_id=delete_event.sender_user_id,
        origin=TurnOrigin.USER_MESSAGE,
    )
    definition = next(tool for tool in tools.definitions(runtime) if tool.name == "memory_change")
    assert definition.parameters["required"] == ["operation"]

    response = json.loads(
        await tools.execute(
            "memory_change",
            json.dumps(
                {
                    "operation": "invalidate",
                    "fact_id": created.new_fact_id,
                    "target": None,
                    "reason": None,
                }
            ),
            runtime,
        )
    )

    assert response["ok"]
    assert response["data"]["outcome"] == "committed"
    fact = await facts.get_fact(created.new_fact_id)
    assert fact is not None and fact.status is MemoryStatus.INVALIDATED


@pytest.mark.asyncio
async def test_autonomous_group_turn_can_create_self_memory_from_current_event(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id="autonomous-group-self-memory",
        sender_user_id="1001",
        group_id="3001",
        content="我们第一次测试了你的自我记忆功能，请把它作为本群经历记住",
    )
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        self_memory_enabled=True,
    )
    tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=facts,
        memory_mutations=service,
        actions=AgentActionRepository(database),
    )
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id=event.sender_user_id),
        text=event.content,
        bot_user_id=event.bot_user_id,
        group_id=event.group_id,
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="group:3001:user:1001",
        trigger_message_id=event.platform_message_id,
        actor_user_id=event.sender_user_id,
        current_group_id=event.group_id,
        origin=TurnOrigin.AUTONOMOUS_GROUP,
    )

    definition = next(tool for tool in tools.definitions(runtime) if tool.name == "memory_change")
    category_schema = definition.parameters["properties"]["category"]  # type: ignore[index]
    assert "self_episode" in category_schema["description"]  # type: ignore[index]
    rejected = json.loads(
        await tools.execute(
            "memory_change",
            json.dumps(
                {
                    "operation": "create",
                    "target": {"subject_ref": "self", "scope_type": "self"},
                    "visibility": "current_scope",
                    "new_content": "Yuki 第一次在本群测试自我记忆功能",
                    "memory_key": "episode:first_self_memory_test",
                    "category": "experience",
                    "kind": "episode",
                    "reason": "Yuki 根据当前真实群消息记录共同经历",
                },
                ensure_ascii=False,
            ),
            runtime,
        )
    )
    assert not rejected["ok"]
    assert rejected["error"] == "invalid_self_memory_category"
    assert rejected["data"]["reason_code"] == "invalid_self_memory_category"
    assert rejected["data"]["allowed_self_categories"] == [
        "self_fact",
        "self_preference",
        "self_episode",
        "self_reflection",
        "self_principle",
    ]
    response = json.loads(
        await tools.execute(
            "memory_change",
            json.dumps(
                {
                    "operation": "create",
                    "target": {"subject_ref": "self", "scope_type": "self"},
                    "visibility": "current_scope",
                    "new_content": "Yuki 第一次在本群测试自我记忆功能",
                    "memory_key": "episode:first_self_memory_test",
                    "category": "self_episode",
                    "kind": "episode",
                    "reason": "Yuki 根据当前真实群消息记录共同经历",
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            ),
            runtime,
        )
    )

    assert response["ok"]
    fact = await facts.get_fact(response["data"]["new_fact_id"])
    assert fact is not None
    assert fact.scope_type is MemoryScopeType.SELF
    assert fact.visibility_type is SelfMemoryVisibility.GROUP
    assert fact.visibility_group_id == "3001"

    self_tool = next(
        tool for tool in tools.definitions(runtime) if tool.name == "get_self_memories"
    )
    assert "Yuki 自己" in self_tool.description
    listed = json.loads(
        await tools.execute(
            "get_self_memories",
            json.dumps({"mode": "overview", "limit": 10}),
            runtime,
        )
    )
    assert listed["ok"]
    assert listed["data"]["visible_scope"] == "global_and_current_group"
    assert [item["fact_id"] for item in listed["data"]["memories"]] == [fact.id]
    serialized = json.dumps(listed["data"], ensure_ascii=False)
    assert "visibility_user_id" not in serialized
    assert "visibility_group_id" not in serialized


@pytest.mark.asyncio
async def test_self_correction_creates_a_new_version(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    original_event = await _event(
        ledger,
        message_id="version-original",
        sender_user_id="1001",
        content="记住我住在北京",
    )
    original = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="住在北京",
            memory_key="location:home",
            category="location",
            reason="original_self_report",
        ),
        _context(original_event),
    )
    assert original.new_fact_id is not None
    correction_event = await _event(
        ledger,
        message_id="version-correction",
        sender_user_id="1001",
        content="我已经搬到上海了",
    )
    correction = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CORRECT,
            fact_id=original.new_fact_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="已经搬到上海",
            reason="current_self_correction",
            expected_fact_state=MemoryStatus.ACTIVE,
        ),
        _context(correction_event),
    )

    assert correction.ok
    assert correction.applied_operation is MemoryMutationAppliedOperation.CORRECT
    assert correction.new_fact_id not in {None, original.new_fact_id}
    old = await facts.get_fact(original.new_fact_id)
    new = await facts.get_fact(correction.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert new is not None and new.status is MemoryStatus.ACTIVE
    assert new.supersedes_id == old.id


@pytest.mark.asyncio
async def test_group_member_can_create_group_and_third_party_group_memory(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    await _event(
        ledger,
        message_id="mentioned-member-presence",
        sender_user_id="2002",
        content="大家好",
        group_id="3001",
    )
    event = await _event(
        ledger,
        message_id="open-group-write",
        sender_user_id="1001",
        content="这个群每周五聚会，@小明 你负责摄影",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    group_result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_group",
                scope_type=MemoryScopeType.GROUP,
            ),
            new_content="每周五聚会",
            memory_key="activity:weekly",
            category="activity",
            reason="group_member_report",
            evidence_quote="这个群每周五聚会",
        ),
        _context(event),
    )
    person_group_result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="mentioned_user",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            new_content="负责摄影",
            memory_key="role:photography",
            category="role",
            reason="third_party_group_report",
            evidence_quote="你负责摄影",
        ),
        _context(event),
    )

    assert group_result.ok and group_result.new_fact_id is not None
    assert person_group_result.ok and person_group_result.new_fact_id is not None
    group_fact = await facts.get_fact(group_result.new_fact_id)
    person_group_fact = await facts.get_fact(person_group_result.new_fact_id)
    assert group_fact is not None and group_fact.authority is MemoryAuthority.GROUP_REPORT
    assert person_group_fact is not None
    assert person_group_fact.authority is MemoryAuthority.THIRD_PARTY
    assert person_group_fact.subject_user_id == "2002"
    assert person_group_fact.group_id == "3001"


@pytest.mark.parametrize(
    "content",
    (
        "江环是@鬼頭桃菜，你记错了",
        "@鬼頭桃菜是江环，这次请按这个主体纠正",
    ),
)
@pytest.mark.asyncio
async def test_mentioned_subject_is_not_rejected_by_chinese_word_order(
    database: Database,
    content: str,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id=f"mentioned-word-order-{hashlib.sha256(content.encode()).hexdigest()[:8]}",
        sender_user_id="1001",
        content=content,
        group_id="3001",
        mentioned_user_ids=("2002",),
    )

    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="mentioned_user",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            new_content="江环是鬼頭桃菜",
            memory_key="identity:jianghuan",
            category="identity",
            evidence_quote=content,
        ),
        _context(event),
    )

    assert result.ok and result.new_fact_id is not None
    fact = await facts.get_fact(result.new_fact_id)
    assert fact is not None
    assert fact.subject_user_id == "2002"
    assert fact.authority is MemoryAuthority.THIRD_PARTY


@pytest.mark.asyncio
async def test_named_member_fuzzy_candidates_can_be_selected_by_agent(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    people = PeopleRepository(database)
    await people.observe(
        user_id="2002",
        nickname="鬼頭桃菜",
        group_id="3001",
        group_card="江环",
    )
    event = await _event(
        ledger,
        message_id="named-member-fuzzy",
        sender_user_id="1001",
        content="江圜是鬼頭桃菜",
        group_id="3001",
    )
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=facts,
        memory_mutations=service,
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=InboundMessage(
            message_id=event.platform_message_id,
            event_type="message",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity(user_id="1001"),
            text=event.content,
            group_id="3001",
            bot_user_id="8000",
        ),
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="group:3001",
        trigger_message_id=event.platform_message_id,
        actor_user_id="1001",
        current_group_id="3001",
        origin=TurnOrigin.USER_MESSAGE,
    )
    arguments = {
        "operation": "create",
        "target": {
            "subject_ref": "named_member",
            "scope_type": "person_group",
            "subject_name": "江圜",
        },
        "new_content": "江环是鬼頭桃菜",
        "memory_key": "identity:jianghuan",
        "category": "identity",
        "evidence_quote": event.content,
    }

    unresolved = json.loads(
        await tools.execute("memory_change", json.dumps(arguments, ensure_ascii=False), runtime)
    )
    assert unresolved["error"] == "subject_resolution_required"
    assert unresolved["retryable"] is True
    assert unresolved["data"]["candidates"][0]["user_id"] == "2002"

    arguments["target"]["candidate_ref"] = "member_candidate_1"
    committed = json.loads(
        await tools.execute("memory_change", json.dumps(arguments, ensure_ascii=False), runtime)
    )
    assert committed["ok"] is True
    fact = await facts.get_fact(committed["data"]["new_fact_id"])
    assert fact is not None and fact.subject_user_id == "2002"


@pytest.mark.asyncio
async def test_reassign_is_one_atomic_versioned_group_operation(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    original_event = await _event(
        ledger,
        message_id="reassign-original",
        sender_user_id="1001",
        content="我喜欢摄影",
        group_id="3001",
    )
    original = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            new_content="喜欢摄影",
            memory_key="hobby:photography",
            category="hobby",
            reason="initial_attribution",
        ),
        _context(original_event),
    )
    assert original.new_fact_id is not None
    event = await _event(
        ledger,
        message_id="reassign-correction",
        sender_user_id="1001",
        content="刚才那条其实说的是小明喜欢摄影",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.REASSIGN,
            fact_id=original.new_fact_id,
            target=MemoryMutationTarget(
                subject_ref="mentioned_user",
                scope_type=MemoryScopeType.PERSON_GROUP,
            ),
            reason="misattributed_subject",
        ),
        _context(event),
    )

    assert result.ok
    assert result.applied_operation is MemoryMutationAppliedOperation.REASSIGN
    assert result.new_fact_id is not None
    old = await facts.get_fact(original.new_fact_id)
    reassigned = await facts.get_fact(result.new_fact_id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED
    assert reassigned is not None and reassigned.status is MemoryStatus.ACTIVE
    assert reassigned.subject_user_id == "2002"
    assert reassigned.group_id == "3001"
    assert reassigned.authority is MemoryAuthority.THIRD_PARTY


@pytest.mark.asyncio
async def test_concurrent_duplicate_requests_commit_once(database: Database) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="concurrent-dedupe",
        sender_user_id="1001",
        content="记住我喜欢爵士乐",
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(
            subject_ref="current_speaker",
            scope_type=MemoryScopeType.PERSON,
        ),
        new_content="喜欢爵士乐",
        memory_key="music:jazz",
        category="music",
        reason="concurrent_same_request",
    )

    first, second = await asyncio.gather(
        service.mutate(request, _context(event)),
        service.mutate(request, _context(event)),
    )

    assert {first.deduplicated, second.deduplicated} == {False, True}
    assert first.mutation_id == second.mutation_id
    assert len(await facts.list_person("1001", limit=20)) == 1


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_fact(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="rollback-receipt",
        sender_user_id="1001",
        content="记住我喜欢蓝色",
    )

    async def fail_finalize(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("forced receipt failure")

    monkeypatch.setattr(service._receipts, "finalize", fail_finalize)
    with pytest.raises(RuntimeError, match="forced receipt failure"):
        await service.mutate(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker",
                    scope_type=MemoryScopeType.PERSON,
                ),
                new_content="喜欢蓝色",
                memory_key="color:favorite",
                category="preference",
                reason="rollback_test",
            ),
            _context(event),
        )

    assert await facts.list_person("1001", limit=20) == ()
    async with database.sessions() as session:
        assert (
            int(
                await session.scalar(select(func.count()).select_from(MemoryMutationReceiptModel))
                or 0
            )
            == 0
        )


@pytest.mark.asyncio
async def test_embedding_schedule_failure_keeps_committed_fact(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="embedding-failure",
        sender_user_id="1001",
        content="记住我喜欢绿色",
    )

    async def fail_embedding(_fact_id: int) -> None:
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(facts, "schedule_embedding", fail_embedding)
    result = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="喜欢绿色",
            memory_key="color:favorite",
            category="preference",
            reason="embedding_failure_test",
        ),
        _context(event),
    )

    assert result.ok and result.new_fact_id is not None
    assert await facts.get_fact(result.new_fact_id) is not None


@pytest.mark.asyncio
async def test_reflection_uses_existing_user_evidence_without_claim_collision(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="reflection-source",
        sender_user_id="1001",
        content="我暂时住在上海",
    )
    created = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            new_content="暂时住在上海",
            memory_key="location:temporary",
            category="location",
            reason="temporary_self_report",
        ),
        _context(event),
    )
    assert created.new_fact_id is not None
    fact = await facts.get_fact(created.new_fact_id)
    assert fact is not None
    reflected = await service.mutate_reflection(
        fact,
        operation=MemoryMutationOperation.INVALIDATE,
        reason=MemoryInvalidationReason.STALE,
    )

    assert reflected.ok
    assert reflected.applied_operation is MemoryMutationAppliedOperation.INVALIDATE
    invalidated = await facts.get_fact(fact.id)
    assert invalidated is not None
    assert invalidated.status is MemoryStatus.INVALIDATED
    assert invalidated.invalidated_reason is MemoryInvalidationReason.STALE


@pytest.mark.asyncio
async def test_merge_metadata_contest_invalidate_and_restore_operations(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)

    async def create(message_id: str, text: str, key: str, content: str) -> int:
        event = await _event(
            ledger,
            message_id=message_id,
            sender_user_id="1001",
            content=text,
        )
        result = await service.mutate(
            MemoryMutationRequest(
                operation=MemoryMutationOperation.CREATE,
                target=MemoryMutationTarget(
                    subject_ref="current_speaker",
                    scope_type=MemoryScopeType.PERSON,
                ),
                new_content=content,
                memory_key=key,
                category="music",
                reason="operation_fixture",
            ),
            _context(event),
        )
        assert result.new_fact_id is not None
        return result.new_fact_id

    source_id = await create(
        "merge-source",
        "我喜欢 Jazz",
        "music:jazz",
        "喜欢 Jazz",
    )
    target_id = await create(
        "merge-target",
        "我喜欢爵士乐",
        "music:favorite",
        "喜欢爵士乐",
    )
    merge_event = await _event(
        ledger,
        message_id="merge-operation",
        sender_user_id="1001",
        content="这两条其实是同一个音乐偏好",
    )
    merged = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.MERGE,
            fact_id=source_id,
            merge_fact_id=target_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="equivalent_music_preferences",
        ),
        _context(merge_event),
    )
    assert merged.ok
    assert merged.applied_operation is MemoryMutationAppliedOperation.MERGE
    assert (await facts.get_fact(source_id)).status is MemoryStatus.SUPERSEDED  # type: ignore[union-attr]

    metadata_event = await _event(
        ledger,
        message_id="metadata-operation",
        sender_user_id="1001",
        content="把它归类为音乐偏好，重要度四级",
    )
    metadata = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.UPDATE_METADATA,
            fact_id=target_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            category="preference",
            kind=MemoryKind.PREFERENCE,
            importance=4,
            reason="metadata_reclassification",
        ),
        _context(metadata_event),
    )
    assert metadata.ok and metadata.new_fact_id is not None
    current_id = metadata.new_fact_id
    current = await facts.get_fact(current_id)
    assert current is not None
    assert current.kind is MemoryKind.PREFERENCE
    assert current.category == "preference"
    assert current.supersedes_id == target_id

    contest_event = await _event(
        ledger,
        message_id="contest-operation",
        sender_user_id="1001",
        content="这条记忆需要先标为有争议",
    )
    contested = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CONTEST,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_requested_review",
        ),
        _context(contest_event),
    )
    assert contested.ok
    assert contested.applied_operation is MemoryMutationAppliedOperation.CONTEST
    assert (await facts.get_fact(current_id)).conflict_state is (  # type: ignore[union-attr]
        MemoryConflictState.CONTESTED
    )

    invalidate_event = await _event(
        ledger,
        message_id="invalidate-operation",
        sender_user_id="1001",
        content="撤销这条音乐偏好记忆",
    )
    invalidated = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_retracted",
        ),
        _context(invalidate_event),
    )
    assert invalidated.ok
    assert (await facts.get_fact(current_id)).status is MemoryStatus.INVALIDATED  # type: ignore[union-attr]

    restore_event = await _event(
        ledger,
        message_id="restore-operation",
        sender_user_id="1001",
        content="恢复这条音乐偏好记忆",
    )
    restored = await service.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.RESTORE,
            fact_id=current_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=MemoryScopeType.PERSON,
            ),
            reason="user_requested_restore",
        ),
        _context(restore_event),
    )
    assert restored.ok
    assert restored.applied_operation is MemoryMutationAppliedOperation.RESTORE
    assert (await facts.get_fact(current_id)).status is MemoryStatus.ACTIVE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mentioned_member_read_is_limited_to_current_group_person_group(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    del service
    current_group_event = await _event(
        ledger,
        message_id="member-in-group",
        sender_user_id="2002",
        content="我喜欢天文",
        group_id="3001",
    )
    global_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="private:secret",
            category="private",
            content="跨群私人事实",
            importance=5,
            confidence=1,
            source_type=MemorySourceType.EXPLICIT,
            authority=MemoryAuthority.EXPLICIT,
        )
    )
    group_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="2002",
            group_id="3001",
            kind=MemoryKind.FACT,
            memory_key="role:photographer",
            category="role",
            content="在本群负责摄影",
            importance=3,
            confidence=0.8,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.THIRD_PARTY,
        )
    )
    projected_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="hobby:astronomy",
            category="hobby",
            content="喜欢天文",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=current_group_event.id,
            source_speaker_user_id="2002",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            confidence=0.9,
            authority=MemoryAuthority.SELF_REPORT,
            excerpt="我喜欢天文",
        ),
    )
    other_group_event = await _event(
        ledger,
        message_id="member-other-group",
        sender_user_id="2002",
        content="我喜欢围棋",
        group_id="3002",
    )
    other_group_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="2002",
            kind=MemoryKind.FACT,
            memory_key="hobby:go",
            category="hobby",
            content="喜欢围棋",
            importance=4,
            confidence=0.9,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.SELF_REPORT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=other_group_event.id,
            source_speaker_user_id="2002",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            confidence=0.9,
            authority=MemoryAuthority.SELF_REPORT,
            excerpt="我喜欢围棋",
        ),
    )
    inbound = InboundMessage(
        message_id="member-read",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001"),
        text="小明在这个群负责什么",
        bot_user_id="8000",
        group_id="3001",
        mentioned_user_ids=("2002",),
    )
    tools = AgentToolService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=ledger,
        memories=facts,
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        actor_user_id="1001",
        current_group_id="3001",
        mentioned_user_ids=("2002",),
    )
    definition = next(
        tool for tool in tools.definitions(runtime) if tool.name == "get_person_memories"
    )
    properties = definition.parameters["properties"]
    assert definition.parameters["required"] == []
    assert set(properties) >= {"subject_ref", "display_name", "user_id"}  # type: ignore[arg-type]
    assert "mentioned_user_1" in properties["subject_ref"]["enum"]  # type: ignore[index]

    by_reference = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"subject_ref": "mentioned_user_1"}),
            runtime,
        )
    )
    reference_ids = {row["fact_id"] for row in by_reference["data"]["memories"]}
    assert by_reference["data"]["resolved_by"] == "subject_ref"
    assert by_reference["data"]["subject_ref"] == "mentioned_user_1"
    assert group_fact.id in reference_ids
    assert projected_fact.id in reference_ids
    assert global_fact.id not in reference_ids
    assert other_group_fact.id not in reference_ids
    projected_row = next(
        row for row in by_reference["data"]["memories"] if row["fact_id"] == projected_fact.id
    )
    assert projected_row["access_scope"] == "same_group_evidence_projection"
    assert projected_row["read_only"] is True

    listed = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"user_id": "2002"}),
            runtime,
        )
    )
    visible_ids = {row["fact_id"] for row in listed["data"]["memories"]}
    assert group_fact.id in visible_ids
    assert projected_fact.id in visible_ids
    assert global_fact.id not in visible_ids
    assert other_group_fact.id not in visible_ids
    queried = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"subject_ref": "mentioned_user_1", "query": "天文"}),
            runtime,
        )
    )
    assert projected_fact.id in {row["fact_id"] for row in queried["data"]["memories"]}
    group_lookup = json.loads(
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": group_fact.id}),
            runtime,
        )
    )
    global_lookup = json.loads(
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": global_fact.id}),
            runtime,
        )
    )
    projected_lookup = json.loads(
        await tools.execute(
            "get_memory_fact",
            json.dumps({"fact_id": projected_fact.id}),
            runtime,
        )
    )
    assert group_lookup["ok"]
    assert not global_lookup["ok"]
    assert not projected_lookup["ok"]


@pytest.mark.asyncio
async def test_manual_qq_and_exact_name_lookup_stay_inside_current_group(
    database: Database,
) -> None:
    _service_unused, facts, ledger, _processor = _service(database)
    people = PeopleRepository(database)
    await people.observe(
        user_id="2002",
        nickname="查无此人",
        group_id="3001",
        group_card="摄影师",
    )
    group_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="2002",
            group_id="3001",
            kind=MemoryKind.FACT,
            memory_key="role:photographer",
            category="role",
            content="在本群负责摄影",
            importance=3,
            confidence=0.8,
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.THIRD_PARTY,
        )
    )
    inbound = InboundMessage(
        message_id="manual-member-read",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001"),
        text="查一下摄影师的记忆",
        bot_user_id="8000",
        group_id="3001",
    )
    tools = AgentToolService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=ledger,
        memories=facts,
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        actor_user_id="1001",
        current_group_id="3001",
    )

    by_qq = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"user_id": "2002"}),
            runtime,
        )
    )
    by_name = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"display_name": "摄影师"}),
            runtime,
        )
    )
    assert by_qq["ok"] and by_qq["data"]["resolved_by"] == "user_id"
    assert by_name["ok"] and by_name["data"]["resolved_by"] == "display_name"
    assert {row["fact_id"] for row in by_qq["data"]["memories"]} == {group_fact.id}
    assert {row["fact_id"] for row in by_name["data"]["memories"]} == {group_fact.id}

    nonmember = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"user_id": "9999"}),
            runtime,
        )
    )
    assert not nonmember["ok"] and nonmember["error"] == "permission_denied"

    await people.observe(
        user_id="2003",
        nickname="另一个人",
        group_id="3001",
        group_card="摄影师",
    )
    ambiguous = json.loads(
        await tools.execute(
            "get_person_memories",
            json.dumps({"display_name": "摄影师"}),
            runtime,
        )
    )
    assert not ambiguous["ok"] and ambiguous["error"] == "ambiguous_person"


@pytest.mark.asyncio
async def test_deterministic_memory_admin_uses_unified_mutation_receipt(
    database: Database,
) -> None:
    service, facts, ledger, _processor = _service(database)
    event = await _event(
        ledger,
        message_id="command-memory-add",
        sender_user_id="1001",
        content="/ai memory add 我喜欢天文",
    )
    admin = MemoryAdminService(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        memories=facts,
        audit=AdminAuditService(database),
        mutations=service,
        ledger=ledger,
    )
    row = await admin.add_memory(
        AdminActor(
            user_id="1001",
            is_superuser=False,
            trigger_message_id=event.platform_message_id,
            conversation_key="private:1001",
            current_message_text=event.content,
            bot_user_id=event.bot_user_id,
            decision_actor_type="command",
        ),
        "1001",
        "我喜欢天文",
    )

    assert row.content == "我喜欢天文"
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryMutationReceiptModel))
    assert receipt is not None
    assert receipt.trigger_event_id == event.id
    assert receipt.decision_actor_type == "command"
    assert receipt.new_fact_id == row.id
