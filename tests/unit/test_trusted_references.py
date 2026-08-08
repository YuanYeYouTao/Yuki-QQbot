"""Main Agent trusted-reference isolation and compact history tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from qq_ai_bot.capabilities.models import CapabilityRisk
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatTool, InboundMessage, SenderIdentity
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.references import (
    MainAgentHistoryProjector,
    ReferenceEpochManager,
    ReferenceResolutionError,
    ReferenceToolAdapter,
)


def _event(
    event_id: int,
    *,
    sender: str,
    message_id: str,
    content: str,
    nickname: str,
    direction: str = "inbound",
    reply_to: str | None = None,
    mentions: tuple[str, ...] = (),
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="999999999",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP,
        sender_user_id=sender,
        direction=direction,
        content=content,
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 8, 10, 0, tzinfo=UTC) + timedelta(seconds=event_id),
        sender_nickname=nickname,
        group_id="888888888",
        mentioned_user_ids=mentions,
        reply_to_message_id=reply_to,
    )


def _fixture():
    events = (
        _event(
            1,
            sender="100000001",
            message_id="900000001",
            content="第一句",
            nickname="同名",
        ),
        _event(
            2,
            sender="100000001",
            message_id="900000002",
            content="第二句",
            nickname="同名",
        ),
        _event(
            3,
            sender="200000002",
            message_id="900000003",
            content="回复并提及",
            nickname="同名",
            reply_to="900000001",
            mentions=("100000001",),
        ),
        _event(
            4,
            sender="999999999",
            message_id="900000004",
            content="收到",
            nickname="Yuki",
            direction="outbound",
        ),
    )
    inbound = InboundMessage(
        message_id="900000005",
        event_type="message:group:normal",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="300000003", nickname="当前用户"),
        text="请处理 @同名",
        raw_text="请处理 @200000002",
        bot_user_id="999999999",
        group_id="888888888",
        mentioned_user_ids=("200000002",),
        reply_to_message_id="900000003",
        reply_sender_user_id="200000002",
    )
    manager = ReferenceEpochManager()
    registry = manager.prepare(
        conversation_key="group:888888888",
        events=events,
        inbound=inbound,
        current_event_id=5,
        anchor_event_id=1,
        reset_marker="none",
    )
    return events, inbound, manager, registry


def test_compact_blocks_preserve_sender_reply_mentions_and_current_boundary() -> None:
    events, inbound, _manager, registry = _fixture()
    projector = MainAgentHistoryProjector(events)

    blocks = projector.project(events, registry)
    current = projector.current_message(
        inbound=inbound,
        content=inbound.text,
        registry=registry,
        current_row=None,
    )
    rendered = "\n".join(block.message.content or "" for block in blocks)

    assert len(blocks) == 3
    assert "[u1=同名#0001]\nm1> 第一句\nm2> 第二句" in rendered
    assert "[u2=同名#0002]\nm3↳m1 @u1> 回复并提及" in rendered
    assert "[Yuki]\nm4> 收到" in rendered
    assert current.content == (
        "[current_event|sender:u3=当前用户#0003|reply:m3|mentions:u2] 请处理 @同名"
    )
    for identifier in (
        "100000001",
        "200000002",
        "300000003",
        "888888888",
        "900000001",
        "900000003",
    ):
        assert identifier not in rendered
        assert identifier not in (current.content or "")

    legacy_renderer = ChatEventPromptRenderer(events)
    legacy_characters = sum(len(legacy_renderer.message(event).content or "") for event in events)
    body_characters = sum(block.body_characters for block in blocks)
    compact_envelope = sum(block.envelope_characters for block in blocks)
    legacy_envelope = legacy_characters - body_characters
    assert compact_envelope <= legacy_envelope * 0.5


def test_epoch_appends_references_without_renumbering() -> None:
    events, inbound, manager, first = _fixture()
    extra = _event(
        6,
        sender="400000004",
        message_id="900000006",
        content="后来加入",
        nickname="新人",
    )
    second = manager.prepare(
        conversation_key="group:888888888",
        events=(*events, extra),
        inbound=inbound,
        current_event_id=7,
        anchor_event_id=1,
        reset_marker="none",
    )

    assert second.epoch_id == first.epoch_id
    assert second.user_for_id("100000001").ref == "u1"  # type: ignore[union-attr]
    assert second.message_for_platform_id("900000001").ref == "m1"  # type: ignore[union-attr]
    assert second.user_for_id("400000004").ref == "u4"  # type: ignore[union-attr]
    assert second.message_for_platform_id("900000006").ref == "m5"  # type: ignore[union-attr]


def test_explicit_identifier_becomes_q_ref_and_old_epoch_tail_is_stale() -> None:
    events, inbound, manager, first = _fixture()
    explicit = InboundMessage(
        message_id="900000007",
        event_type=inbound.event_type,
        scope_type=inbound.scope_type,
        sender=inbound.sender,
        text="请查询 555555555",
        raw_text="请查询 555555555",
        bot_user_id=inbound.bot_user_id,
        group_id=inbound.group_id,
    )
    with_q = manager.prepare(
        conversation_key="group:888888888",
        events=events,
        inbound=explicit,
        current_event_id=7,
        anchor_event_id=1,
        reset_marker="none",
    )
    assert with_q.user("q1").user_id == "555555555"
    assert "555555555" not in str(with_q.model_context())
    assert with_q.project_value({"user_id": "555555555"}) == {"user_ref": "q1"}

    rolled = manager.prepare(
        conversation_key="group:888888888",
        events=events[:1],
        inbound=explicit,
        current_event_id=8,
        anchor_event_id=1,
        reset_marker="none",
        force_roll=True,
    )
    assert rolled.epoch_id != first.epoch_id
    with pytest.raises(ReferenceResolutionError) as stale:
        rolled.user("u3")
    assert stale.value.code.value == "stale_reference"


def test_reference_tool_schema_and_source_policy_are_backend_enforced() -> None:
    _events, _inbound, _manager, registry = _fixture()
    adapter = ReferenceToolAdapter()
    projected = adapter.project_tool(
        ChatTool(
            name="set_example",
            description="example",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "group_id": {"type": "string"},
                },
                "required": ["user_id", "group_id"],
                "additionalProperties": False,
            },
        )
    )
    properties = projected.parameters["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"user_ref", "group_ref"}
    assert projected.parameters["required"] == ["user_ref", "group_ref"]

    readable = adapter.resolve_arguments(
        {"user_ref": "u1", "group_ref": "g1"},
        registry=registry,
        risk=CapabilityRisk.READ,
        tool_name="set_example",
    )
    assert readable == {"user_id": "100000001", "group_id": "888888888"}

    with pytest.raises(ReferenceResolutionError) as historical_mutation:
        adapter.resolve_arguments(
            {"user_ref": "u1", "group_ref": "g1"},
            registry=registry,
            risk=CapabilityRisk.MUTATE,
            tool_name="set_example",
        )
    assert historical_mutation.value.code.value == "target_not_mutable"

    with pytest.raises(ReferenceResolutionError) as sender_mutation:
        adapter.resolve_arguments(
            {"user_ref": "u3"},
            registry=registry,
            risk=CapabilityRisk.MUTATE,
            tool_name="set_example",
        )
    assert sender_mutation.value.code.value == "target_not_mutable"
    assert adapter.resolve_arguments(
        {"user_ref": "u3"},
        registry=registry,
        risk=CapabilityRisk.MUTATE,
        tool_name="self_owned_setting",
        allow_current_sender=True,
    ) == {"user_id": "300000003"}

    mutable = adapter.resolve_arguments(
        {"user_ref": "u2", "group_ref": "g1"},
        registry=registry,
        risk=CapabilityRisk.MUTATE,
        tool_name="set_example",
    )
    assert mutable["user_id"] == "200000002"

    with pytest.raises(ReferenceResolutionError) as raw_identifier:
        adapter.resolve_arguments(
            {"user_id": "200000002"},
            registry=registry,
            risk=CapabilityRisk.READ,
            tool_name="set_example",
        )
    assert raw_identifier.value.code.value == "raw_identifier_not_allowed"

    projected_batch = adapter.project_tool(
        ChatTool(
            name="batch_read",
            description="batch",
            parameters={
                "type": "object",
                "properties": {
                    "target_user_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    }
                },
                "required": ["target_user_ids"],
            },
        )
    )
    batch_properties = projected_batch.parameters["properties"]
    assert isinstance(batch_properties, dict)
    assert "target_user_refs" in batch_properties
    assert adapter.resolve_arguments(
        {"target_user_refs": ["u1", "u2"]},
        registry=registry,
        risk=CapabilityRisk.READ,
        tool_name="batch_read",
    ) == {"target_user_ids": ["100000001", "200000002"]}

    safe_result = adapter.project_result(
        ToolExecutionResult(
            ok=True,
            data={
                "target_user_id": "200000002",
                "group_id": "888888888",
                "message_id": "900000003",
            },
        ),
        registry,
    )
    assert safe_result.data == {
        "target_user_ref": "u2",
        "group_ref": "g1",
        "message_ref": "m3",
    }


def test_output_cleanup_replaces_only_registered_reference_tokens() -> None:
    _events, _inbound, _manager, registry = _fixture()
    cleaned, leaked = registry.clean_output("我会回复 u2 的 m3，普通文本 debug1 保留")

    assert leaked is True
    assert "u2" not in cleaned
    assert "m3" not in cleaned
    assert "debug1" in cleaned

    echoed, echoed_leak = registry.clean_output(
        "前缀 [current_event|sender:u3=当前用户#0003|reply:m3] 正文"
    )
    assert echoed_leak is True
    assert echoed == "前缀 正文"

    user_literal = replace(registry, literal_user_tokens=frozenset({"u2"}))
    preserved, preserved_leak = user_literal.clean_output("变量 u2 保持原样")
    assert preserved_leak is False
    assert preserved == "变量 u2 保持原样"
