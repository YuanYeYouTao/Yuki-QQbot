"""Append-only prompt input reuse across adjacent user turns."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.conftest import make_settings

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.prompting.input_cache import (
    PromptInputCache,
    PromptInputSnapshot,
    splice_appended_input,
)
from qq_ai_bot.prompting.models import CompiledPrompt, PromptMetrics
from qq_ai_bot.prompting.serializer import DYNAMIC_ENVELOPE_HEADER, strip_dynamic_prefix
from qq_ai_bot.services.context_assembler import AssembledContext, ContextMetrics
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.time.models import TimeContext


def _msg(role: str, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def test_strip_dynamic_prefix_returns_event_body() -> None:
    body = "[远野|QQ:1]\n#10>在吗"
    assert strip_dynamic_prefix(f"{DYNAMIC_ENVELOPE_HEADER}[]\n\n{body}") == body
    assert strip_dynamic_prefix(body) == body


def test_splice_appends_previous_current_and_new_assistant() -> None:
    history = (_msg("user", "h1"), _msg("assistant", "h2"))
    current_plain = _msg("user", "在吗")
    current_sent = _msg("user", "动态\n\n在吗")
    previous = PromptInputSnapshot(
        anchor_event_id=10,
        assembler_history=history,
        current_plain=current_plain,
        sent_prefix=history,
        current_sent=current_sent,
    )
    new_history = (*history, current_plain, _msg("assistant", "在呢"))
    new_current = _msg("user", "动态2\n\n你想我了吗")
    spliced = splice_appended_input(
        previous,
        new_history=new_history,
        new_current_sent=new_current,
        rolled=False,
        new_anchor=10,
    )
    assert spliced == (*history, current_sent, _msg("assistant", "在呢"), new_current)


def test_splice_rejects_roll_and_prefix_mismatch() -> None:
    history = (_msg("user", "h1"),)
    previous = PromptInputSnapshot(
        anchor_event_id=10,
        assembler_history=history,
        current_plain=_msg("user", "在吗"),
        sent_prefix=history,
        current_sent=_msg("user", "动态\n\n在吗"),
    )
    assert (
        splice_appended_input(
            previous,
            new_history=(*history, _msg("user", "在吗"), _msg("assistant", "在呢")),
            new_current_sent=_msg("user", "下一句"),
            rolled=True,
            new_anchor=10,
        )
        is None
    )
    assert (
        splice_appended_input(
            previous,
            new_history=(_msg("user", "other"), _msg("user", "在吗")),
            new_current_sent=_msg("user", "下一句"),
            rolled=False,
            new_anchor=10,
        )
        is None
    )


def test_prompt_input_cache_evicts_oldest() -> None:
    cache = PromptInputCache()
    snapshot = PromptInputSnapshot(
        anchor_event_id=1,
        assembler_history=(),
        current_plain=_msg("user", "a"),
        sent_prefix=(),
        current_sent=_msg("user", "a"),
    )
    cache.remember("one", snapshot)
    assert cache.get("one") is snapshot
    cache.forget("one")
    assert cache.get("one") is None


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
    )


def _context(
    *,
    history: tuple[ChatMessage, ...],
    current: ChatMessage,
    rolled: bool = False,
) -> AssembledContext:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    return AssembledContext(
        metadata_payload={},
        history_messages=history,
        current_message=current,
        recent_delivery=(),
        current_time=TimeContext(utc=now, local=now, timezone="UTC"),
        current_relationship=None,
        metrics=ContextMetrics(0, 4, len(history), 2, rolled),
        prompt_cache_key="private:1|reset:none",
        history_anchor_event_id=10,
    )


def test_composer_finalize_appends_second_turn() -> None:
    composer = PromptComposer(make_settings("sqlite+aiosqlite:///:memory:"))
    history = (_msg("user", "h1"), _msg("assistant", "h2"))
    first = composer._finalize(
        _context(history=history, current=_msg("user", "在吗")),
        CompiledPrompt(
            messages=(
                _msg("system", "stable"),
                *history,
                _msg("user", "dyn1\n\n在吗"),
            ),
            selected=(),
            metrics=_metrics(),
        ),
    )
    assert [item.content for item in first] == ["stable", "h1", "h2", "dyn1\n\n在吗"]

    second_history = (*history, _msg("user", "在吗"), _msg("assistant", "在呢"))
    second = composer._finalize(
        _context(history=second_history, current=_msg("user", "你想我了吗")),
        CompiledPrompt(
            messages=(
                _msg("system", "stable"),
                *second_history,
                _msg("user", "dyn2\n\n你想我了吗"),
            ),
            selected=(),
            metrics=_metrics(),
        ),
    )
    assert [item.content for item in second] == [
        "stable",
        "h1",
        "h2",
        "dyn1\n\n在吗",
        "在呢",
        "dyn2\n\n你想我了吗",
    ]
