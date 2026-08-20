"""Identity, lifecycle, queue, and context contracts for Memory V2."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, InboundMessage, SenderIdentity
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.memory.claim_candidates import MemoryClaimCandidateRepository
from qq_ai_bot.memory.enums import (
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryRebuildJobOutcome,
    MemoryRetention,
    MemoryScopeType,
    MemorySourceStyle,
    MemorySourceType,
    MemoryStatus,
    MemorySubjectBasis,
)
from qq_ai_bot.memory.extraction import (
    BatchMemoryClaim,
    BatchMemoryExtractionOutput,
    MemoryClaim,
)
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import SubjectContextBuilder, SubjectResolver
from qq_ai_bot.memory.validation import MemoryClaimValidator
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryJobModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.concurrency import ConcurrencyManager


def _event(
    *,
    event_id: int = 1,
    sender_user_id: str = "1001",
    scope_type: ScopeType = ScopeType.PRIVATE,
    group_id: str | None = None,
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"event-{event_id}",
        scope_type=scope_type,
        sender_user_id=sender_user_id,
        direction="inbound",
        content="我准备考研",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        group_id=group_id,
        private_peer_user_id=sender_user_id if scope_type is ScopeType.PRIVATE else None,
    )


def _claim(**overrides: object) -> MemoryClaim:
    values: dict[str, object] = {
        "subject_ref": "speaker",
        "scope_type": "person",
        "kind": "fact",
        "memory_key": "education:plan",
        "category": "education",
        "content": "准备考研",
        "evidence_quote": "我准备考研",
        "importance": 4,
        "confidence": 0.9,
        "source_type": "automatic",
    }
    values.update(overrides)
    return MemoryClaim.model_validate(values)


def test_production_memory_path_has_no_legacy_semantic_detectors() -> None:
    root = Path(__file__).parents[2] / "src/qq_ai_bot"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            root / "memory/quality_policy.py",
            root / "memory/validation.py",
            root / "memory/subjects.py",
            root / "memory/self_reflection/repository.py",
            root / "memory/self_reflection/service.py",
            root / "services/chat.py",
        )
    )
    for forbidden in (
        "event_requests_memory_mutation",
        "event_requests_explicit_memory",
        "_semantically_anchored",
        "_LEADING_NAMED_OTHER",
        "_NAMED_SUBJECT",
        "_HIGH_VALUE",
        "_PRIVATE_IDENTIFIER",
    ):
        assert forbidden not in source


def test_extraction_schema_rejects_model_selected_identity_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryClaim.model_validate({**_claim().model_dump(), "user_id": "2002"})
    with pytest.raises(ValidationError):
        MemoryClaim.model_validate({**_claim().model_dump(), "source_event_id": 999})


def test_batch_extraction_supports_multiple_claims_per_event() -> None:
    output = BatchMemoryExtractionOutput(
        claims=(
            BatchMemoryClaim(source_event_id=42, claim=_claim()),
            BatchMemoryClaim(
                source_event_id=42,
                claim=_claim(
                    memory_key="pet:cat",
                    category="pet",
                    content="开始养猫",
                    evidence_quote="最近开始养猫",
                ),
            ),
        )
    )

    assert [item.source_event_id for item in output.claims] == [42, 42]
    assert [item.claim.memory_key for item in output.claims] == [
        "education:plan",
        "pet:cat",
    ]


def test_subject_resolver_only_allows_primary_speaker_and_current_group() -> None:
    private = _event()
    group = _event(scope_type=ScopeType.GROUP, group_id="3001")

    assert [item.subject_ref for item in SubjectResolver.available(private)] == ["speaker"]
    assert [item.subject_ref for item in SubjectResolver.available(group)] == [
        "speaker",
        "group",
        "named_member",
    ]
    assert (
        SubjectResolver.resolve(
            private,
            subject_ref="group",
            scope_type=MemoryScopeType.GROUP,
        )
        is None
    )
    assert (
        SubjectResolver.resolve(
            group,
            subject_ref="李四",
            scope_type=MemoryScopeType.PERSON,
        )
        is None
    )


def test_validator_owns_event_and_speaker_identity() -> None:
    event = _event(event_id=42, sender_user_id="1001")
    validated = MemoryClaimValidator().validate(_claim(), event)
    assert validated is not None
    fact, evidence = validated
    assert fact.subject_user_id == "1001"
    assert fact.group_id is None
    assert evidence.event_id == 42
    assert evidence.source_speaker_user_id == "1001"


@pytest.mark.asyncio
async def test_model_declared_group_name_is_resolved_without_parsing_event_text(
    database: Database,
) -> None:
    people = PeopleRepository(database)
    await people.observe(
        user_id="2002",
        nickname="鬼頭桃菜",
        group_id="3001",
        group_card="江环",
    )
    event = replace(
        _event(scope_type=ScopeType.GROUP, group_id="3001"),
        content="这段正文没有固定的姓名谓词格式",
    )
    builder = SubjectContextBuilder(people)
    context = await builder.build(event)
    claims, context = await builder.resolve_claim_names(
        event,
        (
            _claim(
                subject_ref="named_member",
                subject_name="江环",
                scope_type="person_group",
                subject_basis=MemorySubjectBasis.NAMED_UNRESOLVED,
                evidence_quote=event.content,
                content="江环是鬼頭桃菜",
            ),
        ),
        context,
    )

    assert claims[0].subject_ref == "named_1"
    resolved = context.resolve("named_1:person_group")
    assert resolved is not None
    assert resolved.subject_user_id == "2002"


def test_validator_rejects_unknown_subject_and_private_group_claims() -> None:
    event = _event()
    validator = MemoryClaimValidator()
    assert validator.validate(_claim(subject_ref="other_person"), event) is None
    assert (
        validator.validate(
            _claim(subject_ref="group", scope_type="group"),
            event,
        )
        is None
    )


def test_validator_rejects_non_event_evidence_but_trusts_model_paraphrase() -> None:
    event = _event()
    validator = MemoryClaimValidator()
    assert validator.validate(_claim(evidence_quote="上下文里有人准备考研"), event) is None
    assert (
        validator.validate(
            _claim(content="准备出国", evidence_quote="我准备考研"),
            event,
        )
        is not None
    )


def test_validator_allows_grounded_chinese_paraphrase_without_suffix_match() -> None:
    event = replace(_event(), content="我最近开始喜欢喝美式")
    result = MemoryClaimValidator().validate_claim_result(
        _claim(
            content="用户喜欢美式咖啡",
            evidence_quote="我最近开始喜欢喝美式",
        ),
        event,
    )

    assert result.ok
    assert result.claim is not None
    assert result.claim.fact.content == "用户喜欢美式咖啡"


def test_backend_does_not_infer_bot_subject_from_text() -> None:
    text = "Mika 是 CI runner"
    event = replace(_event(), content=text)
    result = MemoryClaimValidator(bot_aliases=("Mika", "米卡")).validate_claim_result(
        _claim(
            content=text,
            evidence_quote=text,
            subject_basis=MemorySubjectBasis.OMITTED_SELF,
        ),
        event,
    )

    assert result.ok


@pytest.mark.parametrize(
    ("text", "content"),
    [
        ("江环是魅魔", "江环是魅魔"),
        ("廉政这爱好倒是挺稳定的，六年前到现在都没变", "廉政的爱好很稳定"),
    ],
)
def test_validator_does_not_parse_named_other_from_prose(
    text: str,
    content: str,
) -> None:
    event = replace(_event(scope_type=ScopeType.GROUP, group_id="3001"), content=text)

    assert (
        MemoryClaimValidator().validate(
            _claim(
                scope_type="person_group",
                content=content,
                evidence_quote=text,
            ),
            event,
        )
        is not None
    )


@pytest.mark.parametrize(
    "text",
    ["我喜欢猫娘", "最近喜欢猫娘", "爱好是摄影", "大家叫我队长"],
)
def test_validator_keeps_first_person_and_subjectless_self_reports(text: str) -> None:
    event = replace(_event(scope_type=ScopeType.GROUP, group_id="3001"), content=text)

    assert (
        MemoryClaimValidator().validate(
            _claim(
                scope_type="person",
                content=text,
                evidence_quote=text,
            ),
            event,
        )
        is not None
    )


def test_memory_kind_comes_from_model_declaration() -> None:
    event = _event()
    event = replace(event, content="以后回复我时请简短一点")
    validated = MemoryClaimValidator().validate_claim(
        _claim(
            content="回复时简短一点",
            evidence_quote="以后回复我时请简短一点",
            kind="preference",
        ),
        event,
    )
    assert validated is not None
    assert validated.fact.kind is MemoryKind.PREFERENCE


@pytest.mark.parametrize(
    ("text", "basis", "expected_reason"),
    [
        ("你今天花了 5.36", MemorySubjectBasis.ADDRESSED_SECOND_PERSON, "speaker_basis"),
        ("Yuki 是 CI runner", MemorySubjectBasis.ABOUT_YUKI, "self_candidate"),
    ],
)
def test_quality_policy_never_falls_second_person_or_yuki_back_to_speaker(
    text: str,
    basis: MemorySubjectBasis,
    expected_reason: str,
) -> None:
    event = replace(_event(), content=text)
    result = MemoryClaimValidator().validate_claim_result(
        _claim(
            content=text,
            evidence_quote=text,
            subject_basis=basis,
        ),
        event,
    )

    assert not result.ok
    assert expected_reason in result.reason_code


@pytest.mark.parametrize(
    ("text", "retention", "style", "reason"),
    [
        ("我去跑步了", MemoryRetention.TRANSIENT, MemorySourceStyle.NATURAL_STATEMENT, "transient"),
        ("请你这轮扮演猫娘", MemoryRetention.DURABLE, MemorySourceStyle.ROLEPLAY, "roleplay"),
        ("获得 36 XP", MemoryRetention.DURABLE, MemorySourceStyle.GENERATED_RESULT, "generated"),
    ],
)
def test_quality_policy_filters_temporary_and_generated_activity(
    text: str,
    retention: MemoryRetention,
    style: MemorySourceStyle,
    reason: str,
) -> None:
    event = replace(_event(), content=text)
    result = MemoryClaimValidator().validate_claim_result(
        _claim(
            content=text,
            evidence_quote=text,
            retention=retention,
            source_style=style,
        ),
        event,
    )

    assert not result.ok
    assert reason in result.reason_code


def test_explicit_request_can_override_retention_but_not_attribution() -> None:
    event = replace(_event(), content="请记住我今天第一次钓到鱼")
    result = MemoryClaimValidator().validate_claim_result(
        _claim(
            kind="episode",
            content="今天第一次钓到鱼",
            evidence_quote="请记住我今天第一次钓到鱼",
            retention=MemoryRetention.TRANSIENT,
            source_style=MemorySourceStyle.INSTRUCTION,
            source_type=MemorySourceType.EXPLICIT,
        ),
        event,
    )

    assert result.ok


async def _append_event(
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    user_id: str = "1001",
    content: str = "我准备考研",
    group_id: str | None = None,
    direction: str = "inbound",
    sender_is_bot: bool = False,
) -> EventRecord:
    row, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender_user_id=user_id,
        direction=direction,
        content=content,
        group_id=group_id,
        private_peer_user_id=None if group_id else user_id,
        sender_is_bot=sender_is_bot,
    )
    return row


def _fact(
    *,
    content: str,
    memory_key: str = "education:plan",
    source_type: MemorySourceType = MemorySourceType.AUTOMATIC,
    user_id: str | None = "1001",
    group_id: str | None = None,
    scope_type: MemoryScopeType = MemoryScopeType.PERSON,
    kind: MemoryKind = MemoryKind.FACT,
) -> MemoryFactCreate:
    return MemoryFactCreate(
        scope_type=scope_type,
        subject_user_id=user_id,
        group_id=group_id,
        kind=kind,
        memory_key=memory_key,
        category="test",
        content=content,
        importance=4,
        confidence=0.9,
        source_type=source_type,
    )


@pytest.mark.asyncio
async def test_same_fact_reuses_active_row_and_accumulates_evidence(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    first_event = await _append_event(ledger, message_id="fact-1")
    second_event = await _append_event(ledger, message_id="fact-2")
    service = MemoryFactService(MemoryFactRepository(database))

    first = await service.remember(
        _fact(content="准备考研"),
        evidence=MemoryEvidenceCreate(
            event_id=first_event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            excerpt="我准备考研",
        ),
    )
    repeated = await service.remember(
        _fact(content="  准备考研\n"),
        evidence=MemoryEvidenceCreate(
            event_id=second_event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            excerpt="还是准备考研",
        ),
    )

    assert repeated.id == first.id
    assert repeated.evidence_count == 2
    assert len(await service.list_person("1001")) == 1


@pytest.mark.asyncio
async def test_changed_fact_supersedes_old_but_automatic_cannot_replace_explicit(
    database: Database,
) -> None:
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    first = await service.remember(_fact(content="准备考研"))
    changed = await service.remember(_fact(content="决定直接工作"))

    assert changed.id != first.id
    assert changed.supersedes_id == first.id
    old = await repository.get_fact(first.id)
    assert old is not None and old.status is MemoryStatus.SUPERSEDED

    explicit = await service.remember(
        _fact(
            content="只喝红茶",
            memory_key="drink:preference",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    rejected = await service.remember(_fact(content="喜欢咖啡", memory_key="drink:preference"))
    assert rejected.id == explicit.id
    assert rejected.content == "只喝红茶"


@pytest.mark.asyncio
async def test_fact_and_evidence_write_rolls_back_as_one_transaction(database: Database) -> None:
    service = MemoryFactService(MemoryFactRepository(database))
    with pytest.raises(IntegrityError):
        await service.remember(
            _fact(content="事务测试"),
            evidence=MemoryEvidenceCreate(
                event_id=999_999,
                source_speaker_user_id="1001",
                relation=MemoryEvidenceRelation.SELF_STATEMENT,
                excerpt="不存在的事件",
            ),
        )
    assert not await service.list_person("1001")


@pytest.mark.asyncio
async def test_jobs_accept_only_real_inbound_non_bot_events(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    inbound = await _append_event(ledger, message_id="job-inbound")
    outbound = await _append_event(
        ledger,
        message_id="job-outbound",
        user_id="8000",
        direction="outbound",
        sender_is_bot=True,
    )
    bot_inbound = await _append_event(
        ledger,
        message_id="job-bot",
        user_id="7000",
        sender_is_bot=True,
    )
    blank = await _append_event(ledger, message_id="job-blank", content="   ")
    jobs = MemoryJobRepository(database)

    assert await jobs.enqueue(inbound.id, "private:1001")
    assert not await jobs.enqueue(inbound.id, "private:1001")
    assert not await jobs.enqueue(outbound.id, "private:1001")
    assert not await jobs.enqueue(bot_inbound.id, "private:7000")
    assert not await jobs.enqueue(blank.id, "private:1001")


@pytest.mark.asyncio
async def test_group_messages_from_different_senders_share_memory_batch_key(
    database: Database,
) -> None:
    harness = build_harness(database, make_settings(database.url))
    await harness.groups.set_enabled("2001", True)
    for index, user_id in enumerate(("1001", "1002"), start=1):
        await harness.processor.handle(
            InboundMessage(
                message_id=f"shared-memory-batch-{index}",
                event_type="message:group:normal",
                scope_type=ScopeType.GROUP,
                sender=SenderIdentity(user_id=user_id, nickname=f"成员{index}"),
                text=f"第 {index} 位成员的消息",
                group_id="2001",
                mentions_bot=True,
                bot_user_id="8000",
            ),
            MemorySender(),
        )
    async with database.sessions() as session:
        rows = (await session.scalars(select(MemoryJobModel).order_by(MemoryJobModel.id))).all()

    assert [row.conversation_key for row in rows] == ["group:2001", "group:2001"]


@pytest.mark.asyncio
async def test_ready_batch_waits_for_twelve_events_and_never_mixes_conversations(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    jobs = MemoryJobRepository(database)
    for index in range(11):
        event = await _append_event(
            ledger,
            message_id=f"batch-count-{index}",
            content=f"消息 {index}",
            group_id="2001",
        )
        assert await jobs.enqueue(event.id, "group:2001")
    other = await _append_event(
        ledger,
        message_id="batch-other-conversation",
        content="另一个群的消息",
        group_id="2002",
    )
    assert await jobs.enqueue(other.id, "group:2002")

    assert not await jobs.claim_ready_batch(
        limit=12,
        trigger_count=12,
        max_characters=8000,
        max_wait_seconds=300,
    )

    twelfth = await _append_event(
        ledger,
        message_id="batch-count-11",
        content="消息 11",
        group_id="2001",
    )
    assert await jobs.enqueue(twelfth.id, "group:2001")
    claimed = await jobs.claim_ready_batch(
        limit=12,
        trigger_count=12,
        max_characters=8000,
        max_wait_seconds=300,
    )

    assert len(claimed) == 12
    assert {job.conversation_key for job in claimed} == {"group:2001"}


@pytest.mark.asyncio
async def test_ready_batch_triggers_on_characters_or_oldest_wait(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    jobs = MemoryJobRepository(database)
    for index in range(2):
        event = await _append_event(
            ledger,
            message_id=f"batch-characters-{index}",
            content=str(index) * 4000,
            group_id="2001",
        )
        assert await jobs.enqueue(event.id, "group:2001")
    characters = await jobs.claim_ready_batch(
        limit=12,
        trigger_count=12,
        max_characters=8000,
        max_wait_seconds=300,
    )
    assert len(characters) == 2
    for job in characters:
        await jobs.complete(job.id, outcome=MemoryRebuildJobOutcome.NO_CLAIMS)

    waiting = await _append_event(
        ledger,
        message_id="batch-oldest-wait",
        content="等待超时后处理",
        group_id="2002",
    )
    assert await jobs.enqueue(waiting.id, "group:2002")
    aged = await jobs.claim_ready_batch(
        limit=12,
        trigger_count=12,
        max_characters=8000,
        max_wait_seconds=300,
        now=datetime.now(UTC) + timedelta(seconds=301),
    )
    assert [job.event_id for job in aged] == [waiting.id]


class _BatchProvider(LLMProvider):
    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        payload = json.loads(request.messages[-1].content or "{}")
        self.inputs.append(payload)
        claims = []
        for event in payload["events"]:
            content = str(event["content"])
            claims.append(
                {
                    "source_event_id": event["source_event_id"],
                    "claim": {
                        "subject_ref": "speaker",
                        "scope_type": "person",
                        "kind": "fact",
                        "memory_key": "primary-event",
                        "category": "test",
                        "content": content,
                        "evidence_quote": content,
                        "importance": 3,
                        "confidence": 0.9,
                        "source_type": "automatic",
                    },
                }
            )
        return ChatResponse(
            content=json.dumps({"claims": claims}, ensure_ascii=False),
            latency_seconds=0,
        )


@pytest.mark.asyncio
async def test_worker_extracts_one_conversation_batch_in_one_model_call(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    first = await _append_event(
        ledger,
        message_id="worker-1",
        user_id="1001",
        content="第一个人的事实",
        group_id="2001",
    )
    second = await _append_event(
        ledger,
        message_id="worker-2",
        user_id="1002",
        content="第二个人的事实",
        group_id="2001",
    )
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(first.id, "group:2001")
    assert await jobs.enqueue(second.id, "group:2001")
    facts = MemoryFactService(MemoryFactRepository(database))
    provider = _BatchProvider()
    worker = MemoryWorker(
        settings=make_settings(
            database.url,
            memory_batch_trigger_count=2,
            memory_batch_max_events=12,
        ),
        jobs=jobs,
        facts=facts,
        ledger=ledger,
        provider=provider,
        concurrency=ConcurrencyManager(1),
    )

    assert await worker.process_once() == 2
    assert len(provider.inputs) == 1
    assert len(provider.inputs[0]["events"]) == 2
    assert [row.content for row in await facts.list_person("1001")] == ["第一个人的事实"]
    assert [row.content for row in await facts.list_person("1002")] == ["第二个人的事实"]
    assert all(len(item["available_subjects"]) >= 2 for item in provider.inputs[0]["events"])


class _CancelledProvider(LLMProvider):
    async def complete(self, request: ChatRequest) -> ChatResponse:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_worker_propagates_cancellation(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="worker-cancel")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:1001")
    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        provider=_CancelledProvider(),
        concurrency=ConcurrencyManager(1),
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.process_once()


class _RejectedClaimProvider(LLMProvider):
    async def complete(self, request: ChatRequest) -> ChatResponse:
        payload = json.loads(request.messages[-1].content or "{}")
        return ChatResponse(
            content=json.dumps(
                {
                    "claims": [
                        {
                            "source_event_id": payload["events"][0]["source_event_id"],
                            "claim": {
                                "subject_ref": "speaker",
                                "scope_type": "person",
                                "kind": "fact",
                                "memory_key": "rejected",
                                "category": "test",
                                "content": "没有证据的内容",
                                "evidence_quote": "原消息中不存在的证据",
                                "importance": 3,
                                "confidence": 0.9,
                                "source_type": "automatic",
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            latency_seconds=0,
        )


@pytest.mark.asyncio
async def test_worker_records_all_rejected_instead_of_no_claims(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="worker-all-rejected")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:1001")
    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        provider=_RejectedClaimProvider(),
        concurrency=ConcurrencyManager(1),
    )

    assert await worker.process_once() == 1
    async with database.sessions() as session:
        row = await session.scalar(
            select(MemoryJobModel).where(MemoryJobModel.event_id == event.id)
        )
    assert row is not None
    assert row.outcome == MemoryRebuildJobOutcome.ALL_REJECTED.value
    assert row.error_category == "all_rejected:evidence_quote_not_in_event"


class _UnknownSourceEventProvider(LLMProvider):
    async def complete(self, request: ChatRequest) -> ChatResponse:
        del request
        return ChatResponse(
            content=json.dumps(
                {
                    "claims": [
                        {
                            "source_event_id": 999_999,
                            "claim": _claim().model_dump(mode="json"),
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            latency_seconds=0,
        )


@pytest.mark.asyncio
async def test_worker_rejects_unknown_batch_event_without_failing_batch(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="worker-unknown-batch-event")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:1001")
    facts = MemoryFactService(MemoryFactRepository(database))
    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=jobs,
        facts=facts,
        ledger=ledger,
        provider=_UnknownSourceEventProvider(),
        concurrency=ConcurrencyManager(1),
    )

    assert await worker.process_once() == 1
    assert not await facts.list_person("1001")
    async with database.sessions() as session:
        row = await session.scalar(
            select(MemoryJobModel).where(MemoryJobModel.event_id == event.id)
        )
    assert row is not None
    assert row.outcome == MemoryRebuildJobOutcome.NO_CLAIMS.value


class _UnexpectedThenValidProvider(_BatchProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        if self.calls == 1:
            raise KeyError("unexpected provider failure")
        return await super().complete(request)


@pytest.mark.asyncio
async def test_worker_requeues_every_job_when_shared_batch_extraction_fails(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    first = await _append_event(
        ledger,
        message_id="worker-batch-failure-1",
        user_id="1001",
        group_id="2001",
    )
    second = await _append_event(
        ledger,
        message_id="worker-batch-failure-2",
        user_id="1002",
        group_id="2001",
    )
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(first.id, "group:2001")
    assert await jobs.enqueue(second.id, "group:2001")
    worker = MemoryWorker(
        settings=make_settings(
            database.url,
            memory_batch_trigger_count=2,
            memory_batch_max_wait_seconds=300,
        ),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        provider=_UnexpectedThenValidProvider(),
        concurrency=ConcurrencyManager(1),
    )

    assert await worker.process_once() == 0
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(MemoryJobModel)
                .where(MemoryJobModel.event_id.in_((first.id, second.id)))
                .order_by(MemoryJobModel.event_id)
            )
        ).all()
    assert [row.status for row in rows] == ["pending", "pending"]
    assert [row.attempts for row in rows] == [1, 1]
    assert [row.error_category for row in rows] == ["KeyError", "KeyError"]


@pytest.mark.asyncio
async def test_worker_isolates_unexpected_job_failure(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    first = await _append_event(ledger, message_id="worker-unexpected-1", user_id="1001")
    second = await _append_event(ledger, message_id="worker-unexpected-2", user_id="1002")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(first.id, "private:1001")
    assert await jobs.enqueue(second.id, "private:1002")
    facts = MemoryFactService(MemoryFactRepository(database))
    worker = MemoryWorker(
        settings=make_settings(
            database.url,
            memory_batch_max_events=12,
            memory_batch_max_wait_seconds=0,
        ),
        jobs=jobs,
        facts=facts,
        ledger=ledger,
        provider=_UnexpectedThenValidProvider(),
        concurrency=ConcurrencyManager(1),
    )

    assert await worker.process_once() == 0
    assert await worker.process_once() == 1
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(MemoryJobModel)
                .where(MemoryJobModel.event_id.in_((first.id, second.id)))
                .order_by(MemoryJobModel.event_id)
            )
        ).all()
    assert rows[0].status == "pending"
    assert rows[0].attempts == 1
    assert rows[0].error_category == "KeyError"
    assert rows[1].status == "done"
    assert [row.content for row in await facts.list_person("1002")] == ["我准备考研"]


@pytest.mark.asyncio
async def test_worker_isolates_job_completion_failure(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = EventLedgerRepository(database)
    first = await _append_event(
        ledger,
        message_id="worker-complete-1",
        user_id="1001",
        group_id="2001",
    )
    second = await _append_event(
        ledger,
        message_id="worker-complete-2",
        user_id="1002",
        group_id="2001",
    )
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(first.id, "group:2001")
    assert await jobs.enqueue(second.id, "group:2001")
    original_complete = jobs.complete
    calls = 0

    async def fail_first_completion(
        job_id: int,
        *,
        outcome: MemoryRebuildJobOutcome = MemoryRebuildJobOutcome.CLAIMS_APPLIED,
        result_category: str | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyError("completion failure")
        await original_complete(
            job_id,
            outcome=outcome,
            result_category=result_category,
        )

    monkeypatch.setattr(jobs, "complete", fail_first_completion)
    worker = MemoryWorker(
        settings=make_settings(
            database.url,
            memory_batch_max_events=12,
            memory_batch_max_wait_seconds=0,
        ),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        provider=_BatchProvider(),
        concurrency=ConcurrencyManager(1),
    )

    assert await worker.process_once() == 1
    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(MemoryJobModel)
                .where(MemoryJobModel.event_id.in_((first.id, second.id)))
                .order_by(MemoryJobModel.event_id)
            )
        ).all()
    assert rows[0].status == "pending"
    assert rows[0].attempts == 1
    assert rows[0].error_category == "KeyError"
    assert rows[1].status == "done"


@pytest.mark.asyncio
async def test_context_keeps_facts_in_current_entity_blocks_only(database: Database) -> None:
    memories = MemoryFactService(MemoryFactRepository(database))
    await memories.remember(_fact(content="只属于当前人物"))
    await memories.remember(
        _fact(
            content="只属于当前群",
            memory_key="group:topic",
            user_id=None,
            group_id="2001",
            scope_type=MemoryScopeType.GROUP,
        )
    )
    await memories.remember(
        _fact(
            content="当前群内称呼",
            memory_key="member:alias",
            group_id="2001",
            scope_type=MemoryScopeType.PERSON_GROUP,
        )
    )
    await memories.remember(
        _fact(
            content="另一个群的秘密",
            memory_key="member:other",
            group_id="2002",
            scope_type=MemoryScopeType.PERSON_GROUP,
        )
    )
    await memories.remember(_fact(content="另一个人的秘密", memory_key="other", user_id="1002"))
    harness = build_harness(
        database,
        make_settings(
            database.url,
            max_context_characters=20_000,
            context_metadata_budget_ratio=0.4,
            memory_automatic_recall_background_limit=10,
            memory_automatic_recall_per_target_limit=10,
        ),
    )
    await harness.groups.set_enabled("2001", True)
    message = InboundMessage(
        message_id="memory-context",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前用户"),
        text="只属于当前人物，只属于当前群，当前群内称呼",
        group_id="2001",
        mentioned_user_ids=("1002",),
        mentions_bot=True,
        bot_user_id="8000",
    )
    await harness.processor.handle(message, MemorySender())
    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    envelope = next(
        item.content or ""
        for item in request.messages
        if '"id":"context.people_and_scene"' in (item.content or "")
    )
    items, _ = json.JSONDecoder().raw_decode(envelope[envelope.index("[") :])
    context = next(item["data"] for item in items if item["id"] == "context.people_and_scene")
    blocks = {item["id"]: item["data"] for item in context["items"]}

    assert [item["content"] for item in blocks["current_person"]["facts"]] == ["只属于当前人物"]
    assert [item["content"] for item in blocks["current_person_in_group"]["facts"]] == [
        "当前群内称呼"
    ]
    assert [item["content"] for item in blocks["current_group"]["facts"]] == ["只属于当前群"]
    assert "另一个人的秘密" not in envelope


@pytest.mark.asyncio
async def test_context_limits_mentioned_member_facts_to_current_group_block(
    database: Database,
) -> None:
    memories = MemoryFactService(MemoryFactRepository(database))
    person_fact = await memories.remember(
        _fact(content="小李喜欢水彩绘画", memory_key="hobby:painting", user_id="1002")
    )
    group_fact = await memories.remember(
        _fact(
            content="小李在本群负责美术",
            memory_key="role:artist",
            user_id="1002",
            group_id="2001",
            scope_type=MemoryScopeType.PERSON_GROUP,
        )
    )
    harness = build_harness(
        database,
        make_settings(
            database.url,
            max_context_characters=20_000,
            context_metadata_budget_ratio=0.25,
        ),
    )
    await harness.groups.set_enabled("2001", True)
    await harness.profiles.upsert(
        user_id="1002",
        nickname="小李",
        group_id="2001",
        group_card="画师小李",
    )
    message = InboundMessage(
        message_id="referenced-memory-context",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001", nickname="当前用户"),
        text="小李喜欢水彩绘画，也在本群负责美术吗",
        group_id="2001",
        mentioned_user_ids=("1002",),
        mentions_bot=True,
        bot_user_id="8000",
    )
    await harness.processor.handle(message, MemorySender())
    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    envelope = next(
        item.content or ""
        for item in request.messages
        if '"id":"context.people_and_scene"' in (item.content or "")
    )
    items, _ = json.JSONDecoder().raw_decode(envelope[envelope.index("[") :])
    context = next(item["data"] for item in items if item["id"] == "context.people_and_scene")
    blocks = {item["id"]: item["data"] for item in context["items"]}
    referenced = blocks["referenced_person.0"]

    assert referenced["user_id"] == "1002"
    assert referenced["person_facts"] == []
    assert [fact["fact_id"] for fact in referenced["group_facts"]] == [group_fact.id]
    assert person_fact.id not in {
        fact["fact_id"]
        for values in (referenced["person_facts"], referenced["group_facts"])
        for fact in values
    }
    assert blocks["current_person"]["facts"] == []
    assert "另一个群的秘密" not in envelope


@pytest.mark.asyncio
async def test_memory_candidate_requires_independent_evidence_and_expires_in_seven_days(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    candidates = MemoryClaimCandidateRepository(database)
    first, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="candidate-one",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="我喜欢浅烘咖啡",
        private_peer_user_id="1001",
    )
    second, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="candidate-two",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="我喜欢浅烘咖啡",
        private_peer_user_id="1001",
    )
    claim = _claim(
        memory_key="preference:coffee-roast",
        category="preference",
        content="喜欢浅烘咖啡",
        evidence_quote="我喜欢浅烘咖啡",
        confidence=0.5,
    )

    once = await candidates.stage(
        claim,
        first,
        candidate_type="memory",
        subject_context=None,
    )
    duplicate = await candidates.stage(
        claim,
        first,
        candidate_type="memory",
        subject_context=None,
    )
    twice = await candidates.stage(
        claim,
        second,
        candidate_type="memory",
        subject_context=None,
    )

    assert once.evidence_count == duplicate.evidence_count == 1
    assert not once.ready_for_promotion
    assert twice.evidence_count == 2
    assert twice.ready_for_promotion
    remaining = twice.expires_at - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7)
