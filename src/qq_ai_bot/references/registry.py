"""Main-Agent-only compact projections built from trusted event records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from qq_ai_bot.domain.messages import ChatMessage, InboundMessage
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.references.models import TurnReferenceRegistry


@dataclass(frozen=True, slots=True)
class MainHistoryBlock:
    """One compact model message that may contain consecutive ledger events."""

    first_event_id: int
    last_event_id: int
    event_ids: tuple[int, ...]
    message: ChatMessage
    body_characters: int
    envelope_characters: int


class MainAgentHistoryProjector:
    """Render compact blocks without exposing backing platform identifiers."""

    def __init__(self, events: tuple[EventRecord, ...]) -> None:
        self._renderer = ChatEventPromptRenderer(events)

    def project(
        self,
        events: tuple[EventRecord, ...],
        registry: TurnReferenceRegistry,
    ) -> tuple[MainHistoryBlock, ...]:
        blocks: list[MainHistoryBlock] = []
        current_rows: list[EventRecord] = []
        current_role = ""
        current_sender = ""

        def flush() -> None:
            if not current_rows:
                return
            block = self._block(tuple(current_rows), registry)
            if block is not None:
                blocks.append(block)
            current_rows.clear()

        previous: EventRecord | None = None
        for row in events:
            role = self._role(row)
            mergeable = row.event_kind != "external_event"
            same_block = bool(
                current_rows
                and mergeable
                and previous is not None
                and previous.event_kind != "external_event"
                and role == current_role
                and row.sender_user_id == current_sender
                and row.occurred_at - previous.occurred_at <= timedelta(minutes=5)
            )
            if not same_block:
                flush()
                current_role = role
                current_sender = row.sender_user_id
            current_rows.append(row)
            previous = row
        flush()
        return tuple(blocks)

    def current_message(
        self,
        *,
        inbound: InboundMessage,
        content: str,
        registry: TurnReferenceRegistry,
        current_row: EventRecord | None,
    ) -> ChatMessage:
        sender = registry.user_for_id(inbound.sender.user_id)
        sender_label = sender.ref if sender is not None else "current_speaker"
        declaration = (
            f"{sender_label}={sender.display_label}" if sender is not None else sender_label
        )
        fields = ["current_event", f"sender:{declaration}"]
        reply = registry.message_for_platform_id(inbound.reply_to_message_id or "")
        if reply is not None:
            fields.append(f"reply:{reply.ref}")
        elif inbound.reply_to_message_id:
            fields.append("reply:outside_window")
        mentions = [
            item.ref
            for user_id in inbound.mentioned_user_ids
            if (item := registry.user_for_id(user_id)) is not None
        ]
        if mentions:
            fields.append(f"mentions:{','.join(dict.fromkeys(mentions))}")
        body = (
            self._renderer.event_content(current_row, inbound.message_id, content)
            if current_row is not None
            else content
        )
        return ChatMessage(
            role="user",
            content=f"[{'|'.join(fields)}] {registry.redact_text(body)}".strip(),
        )

    def _block(
        self,
        rows: tuple[EventRecord, ...],
        registry: TurnReferenceRegistry,
    ) -> MainHistoryBlock | None:
        first = rows[0]
        if first.event_kind == "external_event":
            rendered = registry.redact_text(self._renderer.render_event(first))
            if not rendered.strip():
                return None
            header = "[external_event|untrusted]"
            message = ChatMessage(role="system", content=f"{header}\n{rendered}")
            return MainHistoryBlock(
                first_event_id=first.id,
                last_event_id=first.id,
                event_ids=(first.id,),
                message=message,
                body_characters=len(rendered),
                envelope_characters=len(header) + 1,
            )

        sender = registry.user_for_id(first.sender_user_id)
        header = "[Yuki]" if first.sender_user_id == first.bot_user_id else "[unknown_user]"
        if sender is not None:
            header = f"[{sender.ref}={sender.display_label}]"
        lines: list[str] = []
        visible_event_ids: list[int] = []
        body_characters = 0
        for row in rows:
            message_reference = registry.message_for_platform_id(row.platform_message_id)
            if message_reference is None:
                continue
            body = registry.redact_text(self._renderer.event_content(row, "", ""))
            if not body.strip():
                continue
            relation = ""
            if row.reply_to_message_id:
                target = registry.message_for_platform_id(row.reply_to_message_id)
                relation = f"↳{target.ref}" if target is not None else "↳outside_window"
            mentions = [
                item.ref
                for user_id in row.mentioned_user_ids
                if (item := registry.user_for_id(user_id)) is not None
            ]
            mention_text = f" @{','.join(dict.fromkeys(mentions))}" if mentions else ""
            prefix = f"{message_reference.ref}{relation}{mention_text}> "
            lines.append(f"{prefix}{body}")
            visible_event_ids.append(row.id)
            body_characters += len(body)
        if not lines:
            return None
        content = f"{header}\n" + "\n".join(lines)
        envelope_characters = len(content) - body_characters
        return MainHistoryBlock(
            first_event_id=visible_event_ids[0],
            last_event_id=visible_event_ids[-1],
            event_ids=tuple(visible_event_ids),
            message=ChatMessage(role=self._role(first), content=content),
            body_characters=body_characters,
            envelope_characters=envelope_characters,
        )

    @staticmethod
    def _role(row: EventRecord) -> str:
        if row.event_kind == "external_event":
            return "system"
        return "assistant" if row.direction == "outbound" else "user"


__all__ = ["MainAgentHistoryProjector", "MainHistoryBlock"]
