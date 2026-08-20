"""Compile stable, session, and dynamic prompt contributions once."""

from __future__ import annotations

import hashlib
import math

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.prompting.models import (
    CompiledPrompt,
    PromptContribution,
    PromptMetrics,
    PromptProgram,
    PromptStability,
)
from qq_ai_bot.prompting.serializer import (
    serialize_dynamic,
    serialized_characters,
    serialized_messages_hash,
)


class PromptCompiler:
    """Deduplicate, budget, order, and serialize a prompt program."""

    def compile(
        self,
        program: PromptProgram,
        *,
        history: tuple[ChatMessage, ...] = (),
        current_message: ChatMessage | None = None,
        dynamic_character_budget: int | None = None,
    ) -> CompiledPrompt:
        by_id: dict[str, PromptContribution] = {}
        for contribution in program.contributions:
            if contribution.id in by_id:
                raise ValueError(f"duplicate prompt contribution: {contribution.id}")
            by_id[contribution.id] = contribution
        ordered = tuple(
            sorted(
                by_id.values(),
                key=lambda item: (
                    _stability_rank(item.stability),
                    -item.priority,
                    item.id,
                ),
            )
        )
        static = tuple(item for item in ordered if item.stability is PromptStability.STATIC)
        session = tuple(item for item in ordered if item.stability is PromptStability.SESSION)
        if session:
            raise ValueError("SESSION prompt contributions are not supported in 3.7.0")
        dynamic = tuple(item for item in ordered if item.stability is PromptStability.TURN)
        selected_dynamic = self._select(dynamic, dynamic_character_budget)
        stable_text = "\n\n".join(item.content or "" for item in static)
        dynamic_text = serialize_dynamic(selected_dynamic)
        messages: list[ChatMessage] = []
        if stable_text:
            messages.append(ChatMessage(role="system", content=stable_text))
        messages.extend(history)
        if current_message is not None:
            messages.append(_with_dynamic_prefix(current_message, dynamic_text))
        elif dynamic_text:
            messages.append(ChatMessage(role="user", content=dynamic_text))
        stable_hash = hashlib.sha256(stable_text.encode("utf-8")).hexdigest()
        prefix_messages = (
            tuple(messages[:-1]) if current_message or dynamic_text else tuple(messages)
        )
        conversation_prefix_hash = (
            serialized_messages_hash(prefix_messages) if prefix_messages else ""
        )
        history_characters = sum(len(item.content or "") for item in history)
        current_message_characters = len(current_message.content or "") if current_message else 0
        total_characters = (
            len(stable_text) + len(dynamic_text) + history_characters + current_message_characters
        )
        return CompiledPrompt(
            messages=tuple(messages),
            selected=static + selected_dynamic,
            metrics=PromptMetrics(
                static_characters=len(stable_text),
                dynamic_characters=len(dynamic_text),
                history_characters=history_characters,
                current_message_characters=current_message_characters,
                total_characters=total_characters,
                estimated_tokens=math.ceil(total_characters / 4),
                contribution_count=len(static) + len(selected_dynamic),
                message_count=len(messages),
                stable_prefix_hash=stable_hash,
                session_characters=0,
                conversation_prefix_hash=conversation_prefix_hash,
            ),
        )

    @staticmethod
    def _select(
        contributions: tuple[PromptContribution, ...],
        budget: int | None,
    ) -> tuple[PromptContribution, ...]:
        if budget is None:
            return contributions
        if budget < 0:
            raise ValueError("dynamic prompt budget must not be negative")
        required = tuple(item for item in contributions if item.required)
        used = sum(serialized_characters(item) for item in required)
        if used > budget:
            raise ValueError("required dynamic prompt contributions exceed configured budget")
        selected = list(required)
        for item in contributions:
            if item.required:
                continue
            cost = serialized_characters(item)
            if used + cost <= budget:
                selected.append(item)
                used += cost
        selected_ids = {item.id for item in selected}
        return tuple(item for item in contributions if item.id in selected_ids)


def _stability_rank(stability: PromptStability) -> int:
    if stability is PromptStability.STATIC:
        return 0
    if stability is PromptStability.SESSION:
        return 1
    return 2


def _with_dynamic_prefix(message: ChatMessage, dynamic_text: str) -> ChatMessage:
    """Keep Responses `input` append-only by not inserting a mid-history system turn."""

    if not dynamic_text:
        return message
    body = message.content or ""
    return ChatMessage(
        role=message.role,
        content=f"{dynamic_text}\n\n{body}" if body else dynamic_text,
        tool_calls=message.tool_calls,
        tool_call_id=message.tool_call_id,
        reasoning_content=message.reasoning_content,
    )
