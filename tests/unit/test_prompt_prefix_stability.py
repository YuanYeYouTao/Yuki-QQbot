"""Static prefix hash stays persona-only when SESSION rollup is present."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.conftest import make_settings

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.prompting.compiler import PromptCompiler
from qq_ai_bot.prompting.models import (
    CompiledPrompt,
    PromptChannel,
    PromptContribution,
    PromptMetrics,
    PromptProgram,
    PromptStability,
    PromptTrust,
)
from qq_ai_bot.services.context_assembler import AssembledContext, ContextMetrics
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.time.models import TimeContext


def _metrics() -> PromptMetrics:
    return PromptMetrics(
        static_characters=6,
        dynamic_characters=8,
        history_characters=4,
        current_message_characters=2,
        total_characters=20,
        estimated_tokens=5,
        contribution_count=2,
        message_count=4,
        stable_prefix_hash="abc",
        session_characters=12,
    )


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def test_stable_prefix_hash_ignores_session_and_turn() -> None:
    compiler = PromptCompiler()
    static = PromptContribution(
        id="core.persona",
        channel=PromptChannel.PERSONA,
        trust=PromptTrust.CORE,
        priority=100,
        stability=PromptStability.STATIC,
        content="persona-contract",
        required=True,
    )
    session = PromptContribution(
        id="context.conversation_rollup",
        channel=PromptChannel.CONTEXT,
        trust=PromptTrust.UNTRUSTED,
        priority=70,
        stability=PromptStability.SESSION,
        content="session-frontier",
        required=True,
    )
    turn = PromptContribution(
        id="runtime.time",
        channel=PromptChannel.RUNTIME,
        trust=PromptTrust.TRUSTED,
        payload={"local": "now"},
        required=True,
    )
    without_session = compiler.compile(PromptProgram(contributions=(static, turn)))
    with_session = compiler.compile(PromptProgram(contributions=(static, session, turn)))
    assert without_session.metrics.stable_prefix_hash == with_session.metrics.stable_prefix_hash
    assert with_session.messages[0].content == without_session.messages[0].content
    assert with_session.messages[1].content == "session-frontier"


def test_composer_keeps_session_outside_spliced_history() -> None:
    composer = PromptComposer(make_settings("sqlite+aiosqlite:///:memory:"))
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    history = (_msg("user", "h1"), _msg("assistant", "h2"))
    context = AssembledContext(
        metadata_payload={},
        history_messages=history,
        current_message=_msg("user", "在吗"),
        recent_delivery=(),
        current_time=TimeContext(utc=now, local=now, timezone="UTC"),
        current_relationship=None,
        metrics=ContextMetrics(0, 4, len(history), 2, False, 12, "extractive", 8),
        prompt_cache_key="private:1|reset:none|cov:8|rev:1",
        history_anchor_event_id=10,
        session_text="session-frontier",
    )
    first = composer._finalize(
        context,
        CompiledPrompt(
            messages=(
                _msg("system", "stable"),
                _msg("system", "session-frontier"),
                *history,
                _msg("user", "dyn1\n\n在吗"),
            ),
            selected=(),
            metrics=_metrics(),
        ),
    )
    assert [item.content for item in first] == [
        "stable",
        "session-frontier",
        "h1",
        "h2",
        "dyn1\n\n在吗",
    ]

    second_history = (*history, _msg("user", "在吗"), _msg("assistant", "在呢"))
    second_context = AssembledContext(
        metadata_payload={},
        history_messages=second_history,
        current_message=_msg("user", "你想我了吗"),
        recent_delivery=(),
        current_time=TimeContext(utc=now, local=now, timezone="UTC"),
        current_relationship=None,
        metrics=ContextMetrics(0, 8, len(second_history), 2, False, 12, "extractive", 8),
        prompt_cache_key="private:1|reset:none|cov:8|rev:1",
        history_anchor_event_id=10,
        session_text="session-frontier",
    )
    second = composer._finalize(
        second_context,
        CompiledPrompt(
            messages=(
                _msg("system", "stable"),
                _msg("system", "session-frontier"),
                *second_history,
                _msg("user", "dyn2\n\n你想我了吗"),
            ),
            selected=(),
            metrics=_metrics(),
        ),
    )
    assert [item.content for item in second] == [
        "stable",
        "session-frontier",
        "h1",
        "h2",
        "dyn1\n\n在吗",
        "在呢",
        "dyn2\n\n你想我了吗",
    ]
