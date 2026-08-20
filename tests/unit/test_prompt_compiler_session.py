"""Legacy SESSION contributions are rejected by the 3.7 compiler."""

from __future__ import annotations

import pytest

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.prompting.compiler import PromptCompiler
from qq_ai_bot.prompting.models import (
    PromptChannel,
    PromptContribution,
    PromptProgram,
    PromptStability,
    PromptTrust,
)


def _static() -> PromptContribution:
    return PromptContribution(
        id="core.persona",
        channel=PromptChannel.PERSONA,
        trust=PromptTrust.CORE,
        priority=100,
        stability=PromptStability.STATIC,
        content="persona-contract",
        required=True,
    )


def _session() -> PromptContribution:
    return PromptContribution(
        id="context.conversation_rollup",
        channel=PromptChannel.CONTEXT,
        trust=PromptTrust.UNTRUSTED,
        priority=70,
        stability=PromptStability.SESSION,
        content="session-frontier",
        required=True,
    )


def _turn() -> PromptContribution:
    return PromptContribution(
        id="runtime.time",
        channel=PromptChannel.RUNTIME,
        trust=PromptTrust.TRUSTED,
        priority=10,
        payload={"local": "turn-dynamic"},
        required=True,
    )


def test_session_contribution_is_rejected() -> None:
    compiler = PromptCompiler()
    history = (
        ChatMessage(role="user", content="past user"),
        ChatMessage(role="assistant", content="past assistant"),
    )
    current = ChatMessage(role="user", content="current user")
    program = PromptProgram(contributions=(_static(), _session(), _turn()))
    with pytest.raises(ValueError, match="SESSION"):
        compiler.compile(program, history=history, current_message=current)


def test_session_cannot_be_smuggled_into_current_message() -> None:
    history = (ChatMessage(role="user", content="past"),)
    current = ChatMessage(role="user", content="now")
    program = PromptProgram(contributions=(_static(), _session(), _turn()))
    with pytest.raises(ValueError, match="SESSION"):
        PromptCompiler().compile(program, history=history, current_message=current)


def test_empty_frontier_does_not_emit_second_system() -> None:
    compiled = PromptCompiler().compile(
        PromptProgram(contributions=(_static(), _turn())),
        history=(ChatMessage(role="user", content="past"),),
        current_message=ChatMessage(role="user", content="now"),
    )
    assert [message.role for message in compiled.messages] == ["system", "user", "user"]
    assert compiled.messages[0].content == "persona-contract"
    assert compiled.metrics.session_characters == 0


def test_dynamic_without_current_message_is_never_system_input() -> None:
    compiled = PromptCompiler().compile(
        PromptProgram(contributions=(_static(), _turn())),
    )

    assert [message.role for message in compiled.messages] == ["system", "user"]
    assert "turn-dynamic" in (compiled.messages[1].content or "")
