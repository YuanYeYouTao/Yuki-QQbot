"""Transport-independent domain models."""

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    InboundMessage,
    MessageAttachment,
    OutboundMessage,
    OutboundSendReceipt,
    ReasoningEffort,
    SenderIdentity,
)

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ConversationScope",
    "InboundMessage",
    "MessageAttachment",
    "OutboundMessage",
    "OutboundSendReceipt",
    "ReasoningEffort",
    "ScopeType",
    "SenderIdentity",
]
