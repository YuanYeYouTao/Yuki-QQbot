"""Plan-aware QQ message splitting, pacing, and cooperative supersession."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Protocol

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.conversation.delivery import ReplySequenceSpec
from qq_ai_bot.domain.messages import OutboundMessage, OutboundSendReceipt
from qq_ai_bot.services.renderer import split_qq_message
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    ReplySequenceCancelled,
    TurnToken,
)

logger = logging.getLogger(__name__)

RecordOutbound = Callable[[OutboundMessage, OutboundSendReceipt], Awaitable[None]]
RecordFailure = Callable[[OutboundMessage, Exception], Awaitable[None]]
BeforeSend = Callable[[OutboundMessage], Awaitable[None]]
DeliverOutbound = Callable[[OutboundMessage], Awaitable[OutboundSendReceipt]]
_FENCED_BLOCK = re.compile(r"(```[^\n]*\n.*?\n```|~~~[^\n]*\n.*?\n~~~)", re.DOTALL)
_CHAT_LINE_BREAK = re.compile(r"\n+")
_STRUCTURED_CHAT_OUTPUT = re.compile(
    r"(?m)^[ \t]*(?:```|~~~|[-*+]\s+|\d+[.)、]\s+|>\s+|#{1,6}\s+|\|.*\|[ \t]*$)"
)
_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;])")


class OutboundSender(Protocol):
    async def send(self, message: OutboundMessage) -> OutboundSendReceipt: ...


@dataclass(frozen=True, slots=True)
class DeliveryFailureRecovery:
    """A narrowly scoped replacement for one failed reply-effect delivery."""

    handled: bool
    replacement_messages: tuple[OutboundMessage, ...] = ()


RecoverDeliveryFailure = Callable[[OutboundMessage, Exception], Awaitable[DeliveryFailureRecovery]]


@dataclass(frozen=True, slots=True)
class ReplySequenceResult:
    planned_messages: int
    sent_messages: int
    cancelled: bool


class ReplySequenceManager:
    """Own the full post-generation delivery sequence outside ChatService."""

    def __init__(
        self,
        coordinator: ConversationTurnCoordinator,
        *,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._coordinator = coordinator
        self._random_uniform = random_uniform

    def render(
        self,
        text: str,
        *,
        spec: ReplySequenceSpec,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[str, ...]:
        hard_max = min(spec.max_messages, runtime.reply.hard_max_messages)
        structured = _STRUCTURED_CHAT_OUTPUT.search(text) is not None
        if spec.split_hint == "paragraph":
            messages = self._split_chat_line_sections(text, max_messages=hard_max)
        elif spec.split_hint == "sentence":
            messages = self._split_sentences(text, max_messages=hard_max)
        elif structured:
            messages = (text,)
        else:
            messages = self._split_chat_line_sections(text, max_messages=hard_max)
        chunks = tuple(
            chunk
            for message in messages
            for chunk in self._split_preserving_structure(
                message,
                limit=runtime.reply.max_qq_message_chars,
            )
        )
        if len(chunks) <= hard_max:
            return chunks
        # MAX_OUTPUT_CHARACTERS should normally keep this unreachable.  Preserve
        # earlier complete chunks and combine the tail rather than dropping text.
        return (*chunks[: hard_max - 1], "\n".join(chunks[hard_max - 1 :]))

    @staticmethod
    def _split_chat_line_sections(text: str, *, max_messages: int) -> tuple[str, ...]:
        """Treat model-authored line breaks as ordinary-chat send boundaries."""

        sections = tuple(
            section.strip() for section in _CHAT_LINE_BREAK.split(text) if section.strip()
        )
        if len(sections) < 2:
            return (text,) if text else ()
        if len(sections) <= max_messages:
            return sections
        groups: list[list[str]] = [[] for _ in range(max_messages)]
        for index, section in enumerate(sections):
            group_index = min(index * max_messages // len(sections), max_messages - 1)
            groups[group_index].append(section)
        # The hard cap wins when the model emits too many lines; retained line
        # breaks keep the merged overflow readable without dropping content.
        return tuple("\n".join(group) for group in groups if group)

    @staticmethod
    def _split_sentences(text: str, *, max_messages: int) -> tuple[str, ...]:
        parts = tuple(part.strip() for part in _SENTENCE_BREAK.split(text) if part.strip())
        if len(parts) < 2:
            return (text,) if text else ()
        if len(parts) <= max_messages:
            return parts
        return ReplySequenceManager._split_chat_line_sections(
            "\n".join(parts),
            max_messages=max_messages,
        )

    async def send(
        self,
        *,
        text: str,
        spec: ReplySequenceSpec,
        runtime: RuntimeConfigSnapshot,
        token: TurnToken,
        sender: OutboundSender,
        record_outbound: RecordOutbound,
        record_failure: RecordFailure | None = None,
        before_send: BeforeSend | None = None,
        deliver_outbound: DeliverOutbound | None = None,
        recover_failure: RecoverDeliveryFailure | None = None,
        before_messages: tuple[OutboundMessage, ...] = (),
        after_messages: tuple[OutboundMessage, ...] = (),
        suppress_text: bool = False,
        reply_to_message_id: str | None = None,
    ) -> ReplySequenceResult:
        chunks = () if suppress_text else self.render(text, spec=spec, runtime=runtime)
        outbound_messages = [*before_messages]
        outbound_messages.extend(OutboundMessage(text=chunk) for chunk in chunks)
        outbound_messages.extend(after_messages)
        if reply_to_message_id is not None and outbound_messages:
            outbound_messages[0] = replace(
                outbound_messages[0],
                reply_to_message_id=reply_to_message_id,
            )
        sent = 0
        try:
            async with self._coordinator.track(token, "reply"):
                for index, outbound in enumerate(outbound_messages):
                    if runtime.reply.cancel_on_new_message and not self._coordinator.is_current(
                        token
                    ):
                        raise ReplySequenceCancelled("newer message superseded reply sequence")
                    if index > 0:
                        delay = self._random_uniform(
                            runtime.reply.delay_min_seconds,
                            runtime.reply.delay_max_seconds,
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                    try:
                        if before_send is not None and deliver_outbound is None:
                            await before_send(outbound)
                        receipt = (
                            await deliver_outbound(outbound)
                            if deliver_outbound is not None
                            else await sender.send(outbound)
                        )
                        if not isinstance(receipt, OutboundSendReceipt):
                            raise TypeError("outbound sender returned no delivery receipt")
                    except Exception as exc:
                        failure_message = outbound
                        failure = exc
                        if outbound.reply_to_message_id is not None:
                            failure_message = replace(outbound, reply_to_message_id=None)
                            logger.warning(
                                "reply_quote_delivery_failed retry_without_quote=true "
                                "exception_category=%s",
                                type(exc).__name__,
                            )
                            try:
                                receipt = (
                                    await deliver_outbound(failure_message)
                                    if deliver_outbound is not None
                                    else await sender.send(failure_message)
                                )
                                if not isinstance(receipt, OutboundSendReceipt):
                                    raise TypeError("outbound sender returned no delivery receipt")
                            except Exception as retry_exc:
                                failure = retry_exc
                            else:
                                sent += 1
                                if deliver_outbound is None:
                                    await self._record_after_acceptance(
                                        failure_message,
                                        receipt,
                                        record_outbound,
                                    )
                                continue
                        if record_failure is not None:
                            await record_failure(failure_message, failure)
                        recovery = (
                            await recover_failure(failure_message, failure)
                            if recover_failure is not None
                            else DeliveryFailureRecovery(handled=False)
                        )
                        if not recovery.handled:
                            if failure is exc:
                                raise
                            raise failure from exc
                        for replacement in recovery.replacement_messages:
                            replacement_receipt = (
                                await deliver_outbound(replacement)
                                if deliver_outbound is not None
                                else await sender.send(replacement)
                            )
                            if not isinstance(replacement_receipt, OutboundSendReceipt):
                                raise TypeError(
                                    "outbound sender returned no delivery receipt"
                                ) from exc
                            sent += 1
                            if deliver_outbound is None:
                                await self._record_after_acceptance(
                                    replacement,
                                    replacement_receipt,
                                    record_outbound,
                                )
                        continue
                    sent += 1
                    if deliver_outbound is None:
                        await self._record_after_acceptance(outbound, receipt, record_outbound)
        except ReplySequenceCancelled:
            return ReplySequenceResult(len(outbound_messages), sent, True)
        return ReplySequenceResult(len(outbound_messages), sent, False)

    @staticmethod
    async def _record_after_acceptance(
        message: OutboundMessage,
        receipt: OutboundSendReceipt,
        record_outbound: RecordOutbound,
    ) -> None:
        """Never turn post-send persistence failure into a transport retry."""

        try:
            await record_outbound(message, receipt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "reply_post_send_record_failed exception_category=%s",
                type(exc).__name__,
            )

    @staticmethod
    def _split_preserving_structure(text: str, *, limit: int) -> tuple[str, ...]:
        if len(text) <= limit:
            return (text,) if text else ()
        parts = _FENCED_BLOCK.split(text)
        chunks: list[str] = []
        for part in parts:
            if not part:
                continue
            if part.startswith(("```", "~~~")):
                chunks.extend(ReplySequenceManager._split_fenced_block(part, limit=limit))
            else:
                chunks.extend(split_qq_message(part, limit=limit))
        return tuple(chunk for chunk in chunks if chunk.strip())

    @staticmethod
    def _split_fenced_block(block: str, *, limit: int) -> tuple[str, ...]:
        lines = block.splitlines()
        if len(block) <= limit or len(lines) < 3:
            return (block,)
        opening = lines[0]
        closing = lines[-1]
        body = lines[1:-1]
        available = max(1, limit - len(opening) - len(closing) - 2)
        chunks: list[str] = []
        current: list[str] = []
        current_length = 0
        for line in body:
            extra = len(line) + int(bool(current))
            if current and current_length + extra > available:
                chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
                current = []
                current_length = 0
            if len(line) > available:
                if current:
                    chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
                    current = []
                    current_length = 0
                for index in range(0, len(line), available):
                    chunks.append(f"{opening}\n{line[index : index + available]}\n{closing}")
                continue
            current.append(line)
            current_length += extra
        if current:
            chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
        return tuple(chunks)
