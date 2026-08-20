"""Thin Conversation Runtime host that never owns a second ChatService."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.runtime.turn import TurnContext
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.turn_coordinator import TurnToken
from qq_ai_bot.vision.models import VisualObservation


class HostConversationRuntime:
    """Production ConversationRuntime entry; session work stays in ChatService."""

    def __init__(self, chat: ChatService) -> None:
        self._chat = chat

    async def begin_turn(self, context: TurnContext) -> object:
        del context
        raise NotImplementedError(
            "use handle_turn(); ChatService.respond owns prepare/run_agent/deliver"
        )

    async def handle_turn(
        self,
        inbound: InboundMessage,
        identity: ConversationScope,
        profile: UserProfileSnapshot,
        content: str,
        sender: OutboundSender,
        *,
        autonomous: bool = False,
        runtime_snapshot: RuntimeConfigSnapshot | None = None,
        visual_observation: VisualObservation | None = None,
        visual_input_present: bool = False,
        visual_failure: bool = False,
        turn_token: TurnToken | None = None,
        **kwargs: Any,
    ) -> int:
        """Reply-producing turn without PlannedTurn."""

        return await self._chat.respond(
            inbound,
            identity,
            profile,
            content,
            sender,
            autonomous=autonomous,
            runtime_snapshot=runtime_snapshot,
            visual_observation=visual_observation,
            visual_input_present=visual_input_present,
            visual_failure=visual_failure,
            turn_token=turn_token,
            **kwargs,
        )
