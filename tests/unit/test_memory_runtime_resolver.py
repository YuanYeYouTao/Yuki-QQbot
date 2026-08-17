"""Unit tests for the deterministic memory access resolver (R2 commit 2)."""

from __future__ import annotations

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryContextPolicy,
    MemoryReadPolicy,
    MemoryWritePolicy,
    MemoryWriteTransition,
)
from qq_ai_bot.memory.runtime.resolver import (
    MemoryAccessReason,
    MemoryStructuredCommand,
    resolve_memory_access,
)
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.origin import TurnOrigin


def _authority(origin: TurnOrigin = TurnOrigin.USER_MESSAGE) -> TurnAuthority:
    return TurnAuthority(
        actor_user_id="u-1",
        bot_user_id="bot-9",
        origin=origin,
        permission_ceiling=frozenset({"user"}),
        delegated_authority=None,
        authority_revision=1,
    )


def _scene(*, image: bool = False, reply: bool = False) -> TurnSceneFacts:
    return TurnSceneFacts(
        scope_type=ScopeType.PRIVATE,
        group_id=None,
        image_present=image,
        reply_present=reply,
    )


class TestOrdinaryLanguage:
    def test_plain_message_is_passive_background(self) -> None:
        decision = resolve_memory_access(authority=_authority(), scene=_scene())
        assert decision.reason is MemoryAccessReason.ORDINARY_NATURAL_LANGUAGE
        assert decision.contract.context_policy is MemoryContextPolicy.BACKGROUND
        assert decision.contract.read_policy is MemoryReadPolicy.DEFERRED
        assert decision.contract.write_transition is MemoryWriteTransition.REQUESTABLE
        assert decision.retrieval_degraded is False

    def test_trusted_reply_selects_continuation_without_reading_text(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=_scene(reply=True),
        )
        assert decision.contract.default_purpose is MemoryRecallPurpose.CONTINUATION
        assert decision.contract.context_policy is MemoryContextPolicy.CONTINUATION

    def test_retrieval_disabled_is_degraded_not_forbidden(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=_scene(),
            retrieval_enabled=False,
        )
        assert decision.contract.availability is MemoryAvailability.ENABLED
        assert decision.retrieval_degraded is True


class TestImageWriteTightening:
    def test_image_turn_keeps_context_and_denies_write(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=_scene(image=True),
        )
        assert decision.reason is MemoryAccessReason.IMAGE_WRITE_DISABLED
        assert decision.contract.context_policy is MemoryContextPolicy.BACKGROUND
        assert decision.contract.write_transition is MemoryWriteTransition.DENIED
        assert decision.contract.write_policy is MemoryWritePolicy.DISABLED

    def test_structured_write_on_image_does_not_enter_exclusive(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=_scene(image=True),
            structured_command=MemoryStructuredCommand.WRITE,
        )
        assert decision.reason is MemoryAccessReason.IMAGE_WRITE_DISABLED
        assert decision.contract.write_policy is MemoryWritePolicy.DISABLED


class TestStructuredCommands:
    def test_structured_write_becomes_exclusive(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=_scene(),
            structured_command=MemoryStructuredCommand.WRITE,
        )
        assert decision.reason is MemoryAccessReason.STRUCTURED_WRITE_COMMAND
        assert decision.contract.write_policy is MemoryWritePolicy.EXCLUSIVE

    def test_structured_read_is_eager_without_automatic_inject(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=_scene(),
            structured_command=MemoryStructuredCommand.READ,
        )
        assert decision.reason is MemoryAccessReason.STRUCTURED_READ_COMMAND
        assert decision.contract.read_policy is MemoryReadPolicy.EAGER
        assert decision.contract.context_policy is MemoryContextPolicy.NONE
        assert decision.contract.write_transition is MemoryWriteTransition.REQUESTABLE


class TestOriginAndAuthority:
    def test_unavailable_memory_is_forbidden(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(),
            scene=_scene(),
            memory_available=False,
        )
        assert decision.reason is MemoryAccessReason.AUTHORITY_FORBIDDEN
        assert decision.contract.availability is MemoryAvailability.FORBIDDEN

    def test_plugin_background_is_dormant_and_cannot_write(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(TurnOrigin.PLUGIN_BACKGROUND),
            scene=_scene(),
        )
        assert decision.reason is MemoryAccessReason.ORIGIN_RESTRICTED
        assert decision.contract.context_policy is MemoryContextPolicy.NONE
        assert decision.contract.read_policy is MemoryReadPolicy.DEFERRED
        assert decision.contract.write_transition is MemoryWriteTransition.DENIED

    def test_scheduled_automation_cannot_write(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(TurnOrigin.SCHEDULED_AUTOMATION),
            scene=_scene(),
            structured_command=MemoryStructuredCommand.WRITE,
        )
        assert decision.contract.write_policy is MemoryWritePolicy.DISABLED
        assert decision.reason is MemoryAccessReason.ORIGIN_RESTRICTED

    def test_autonomous_group_cannot_persist_write(self) -> None:
        decision = resolve_memory_access(
            authority=_authority(TurnOrigin.AUTONOMOUS_GROUP),
            scene=_scene(),
        )
        assert decision.contract.write_transition is MemoryWriteTransition.DENIED
        assert decision.reason is MemoryAccessReason.ORIGIN_WRITE_DENIED
