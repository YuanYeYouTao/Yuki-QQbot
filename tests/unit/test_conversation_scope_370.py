from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest

from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.runtime.keys import ResolvedMemoryScope, TurnCoordinationKey
from qq_ai_bot.services.effect_gate import (
    ConversationEffectGate,
    EffectGateTimeoutError,
    EffectPermitRejectedError,
)

_REPO_ROOT = Path(__file__).parents[2]


def _cleared_runtime_symbols() -> tuple[str, ...]:
    return (
        "Conversation" + "Mode",
        "PER" + "_USER",
        "SHAR" + "ED",
        "context" + "_reset",
        "set_" + "context_" + "reset",
        "Context" + "ResetModel",
        "Conversation" + "HistoryIdentity",
        "Conversation" + "HistoryState",
        "History" + "SummaryStatus",
        "History" + "MemberType",
        "SUMMARY" + "_ROLLUP",
        "active_frontier" + "_end_event_id",
        "reset" + "_at",
        "set_history" + "_observer",
        "Prompt" + "InputCache",
        "Prompt" + "InputSnapshot",
        "splice_appended" + "_input",
        "prompt_cache" + "_key",
        "conversation_history_rollup_" + "max_attempts",
        "conversation_history_rollup_" + "l0_",
        "conversation_history_rollup_" + "fan_in",
        "conversation_history_rollup_" + "max_level",
    )


def test_removed_runtime_symbols_are_absent_from_source_and_tests() -> None:
    violations: list[str] = []
    for root in (_REPO_ROOT / "src" / "qq_ai_bot", _REPO_ROOT / "tests"):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for symbol in _cleared_runtime_symbols():
                pattern = rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])"
                if re.search(pattern, text):
                    violations.append(f"{path.relative_to(_REPO_ROOT)}:{symbol}")
    assert not violations, "removed 3.6 runtime symbols remain:\n" + "\n".join(violations)


def test_chat_event_model_is_only_constructed_by_scoped_uow() -> None:
    allowed = Path("src/qq_ai_bot/persistence/scoped_event_uow.py")
    violations: list[str] = []
    source_root = _REPO_ROOT / "src" / "qq_ai_bot"
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT)
        if relative == allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            direct_constructor = (
                isinstance(node.func, ast.Name) and node.func.id == "ChatEventModel"
            )
            insert_call = (
                isinstance(node.func, ast.Name)
                and node.func.id == "insert"
                and any(
                    isinstance(argument, ast.Name) and argument.id == "ChatEventModel"
                    for argument in node.args
                )
            )
            if direct_constructor or insert_call:
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, "direct chat ledger writes bypass scoped UoW:\n" + "\n".join(violations)


def test_group_scope_is_bot_aware_and_actor_free() -> None:
    first = ConversationScope.group("bot-1", "group-1")
    second_actor = ConversationScope.group("bot-1", "group-1")
    other_bot = ConversationScope.group("bot-2", "group-1")

    assert first == second_actor
    assert first.key == "bot:bot-1:group:group-1"
    assert other_bot.key != first.key
    assert not hasattr(first, "user_id")


def test_private_scope_and_coordination_key_are_identical_but_memory_key_is_not() -> None:
    scope = ConversationScope.private("bot-1", "peer-1")
    coordination = TurnCoordinationKey.from_scope(scope)
    memory = ResolvedMemoryScope.for_private("peer-1")

    assert coordination.partition_key == scope.key
    assert memory.partition_key == "private:peer-1"
    assert memory.partition_key != scope.key


@pytest.mark.asyncio
async def test_effect_gate_validates_inside_lock_and_issues_unique_permits() -> None:
    gate = ConversationEffectGate()
    snapshot = ConversationTurnSnapshot(1, "bot:b:group:g", 2, 10, 3)
    permits: list[str] = []

    async def valid(candidate: ConversationTurnSnapshot) -> bool:
        return candidate == snapshot

    async with gate.permit(snapshot, validate=valid, timeout_seconds=1) as permit:
        permits.append(permit.effect_id)
    async with gate.permit(snapshot, validate=valid, timeout_seconds=1) as permit:
        permits.append(permit.effect_id)

    assert permits[0] != permits[1]


@pytest.mark.asyncio
async def test_effect_gate_rejects_stale_fence_and_times_out() -> None:
    gate = ConversationEffectGate()
    snapshot = ConversationTurnSnapshot(1, "bot:b:group:g", 2, 10, 3)

    async def stale(_: ConversationTurnSnapshot) -> bool:
        return False

    with pytest.raises(EffectPermitRejectedError):
        async with gate.permit(snapshot, validate=stale, timeout_seconds=1):
            pass

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocker() -> None:
        async with gate.hold(snapshot.scope_key, timeout_seconds=1):
            entered.set()
            await release.wait()

    task = asyncio.create_task(blocker())
    await entered.wait()
    try:
        with pytest.raises(EffectGateTimeoutError):
            async with gate.hold(snapshot.scope_key, timeout_seconds=0.01):
                pass
    finally:
        release.set()
        await task
