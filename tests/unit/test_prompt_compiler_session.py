"""SESSION contributions compile to a second system, never the current-turn prefix."""

from __future__ import annotations

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.prompting.compiler import PromptCompiler, _with_dynamic_prefix
from qq_ai_bot.prompting.models import (
    PromptChannel,
    PromptContribution,
    PromptProgram,
    PromptStability,
    PromptTrust,
)
from qq_ai_bot.prompting.serializer import serialize_dynamic


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


def _legacy_binary_compile(
    program: PromptProgram,
    *,
    history: tuple[ChatMessage, ...],
    current_message: ChatMessage,
):
    """Pre-3.6.1 compiler: every non-STATIC contribution is current-message prefix."""

    static = tuple(
        item for item in program.contributions if item.stability is PromptStability.STATIC
    )
    dynamic = tuple(
        item for item in program.contributions if item.stability is not PromptStability.STATIC
    )
    stable_text = "\n\n".join(item.content or "" for item in static)
    dynamic_text = serialize_dynamic(dynamic)
    messages = []
    if stable_text:
        messages.append(ChatMessage(role="system", content=stable_text))
    messages.extend(history)
    messages.append(_with_dynamic_prefix(current_message, dynamic_text))
    return tuple(messages)


def test_session_is_second_system_not_current_body() -> None:
    compiler = PromptCompiler()
    history = (
        ChatMessage(role="user", content="past user"),
        ChatMessage(role="assistant", content="past assistant"),
    )
    current = ChatMessage(role="user", content="current user")
    program = PromptProgram(contributions=(_static(), _session(), _turn()))
    compiled = compiler.compile(program, history=history, current_message=current)

    assert [message.role for message in compiled.messages] == [
        "system",
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert compiled.messages[0].content == "persona-contract"
    assert compiled.messages[1].content == "session-frontier"
    assert compiled.messages[2].content == "past user"
    current_body = compiled.messages[-1].content or ""
    assert current_body.endswith("current user")
    assert "session-frontier" not in current_body
    assert '"id":"runtime.time"' in current_body
    assert compiled.metrics.session_characters == len("session-frontier")
    assert compiled.metrics.dynamic_characters == len(serialize_dynamic((_turn(),)))


def test_binary_compile_would_smuggle_session_into_current_message() -> None:
    history = (ChatMessage(role="user", content="past"),)
    current = ChatMessage(role="user", content="now")
    program = PromptProgram(contributions=(_static(), _session(), _turn()))
    legacy = _legacy_binary_compile(program, history=history, current_message=current)
    assert legacy[0].content == "persona-contract"
    assert "session-frontier" in (legacy[-1].content or "")
    compiled = PromptCompiler().compile(program, history=history, current_message=current)
    assert compiled.messages[1].content == "session-frontier"
    assert "session-frontier" not in (compiled.messages[-1].content or "")


def test_empty_frontier_does_not_emit_second_system() -> None:
    compiled = PromptCompiler().compile(
        PromptProgram(contributions=(_static(), _turn())),
        history=(ChatMessage(role="user", content="past"),),
        current_message=ChatMessage(role="user", content="now"),
    )
    assert [message.role for message in compiled.messages] == ["system", "user", "user"]
    assert compiled.messages[0].content == "persona-contract"
    assert compiled.metrics.session_characters == 0
