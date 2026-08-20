"""Message models shared by adapters and business services."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from qq_ai_bot.domain.conversations import ConversationScope, ScopeType


def sanitize_display_name(value: str) -> str:
    """Flatten untrusted platform identity text for one-line model metadata."""

    visible = "".join(
        " " if character.isspace() else character
        for character in value
        if not unicodedata.category(character).startswith("C")
    )
    return " ".join(visible.split())[:128]


class AttachmentKind(StrEnum):
    """Attachment kinds intentionally not downloaded by the MVP."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    FORWARD = "forward"
    CARD = "card"
    UNKNOWN = "unknown"


class ReasoningEffort(StrEnum):
    """Provider-neutral reasoning depth supported by DeepSeek V4."""

    HIGH = "high"
    MAX = "max"


class NativeToolType(StrEnum):
    """Provider-executed tools supported by the compatibility layer."""

    WEB_SEARCH = "web_search"


class NativeToolStatus(StrEnum):
    """Normalized lifecycle state for a provider-executed tool."""

    IN_PROGRESS = "in_progress"
    SEARCHING = "searching"
    COMPLETED = "completed"
    FAILED = "failed"


class CitationOrigin(StrEnum):
    """Where a provider-neutral source URL was recovered."""

    ANNOTATION = "annotation"
    OPEN_PAGE_ACTION = "open_page_action"
    ANSWER_TEXT = "answer_text"


class ModelResponseStatus(StrEnum):
    """Successful provider response states returned to the Agent loop."""

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class ProviderContinuation:
    """Opaque provider-owned state that may only live inside one Agent turn."""

    provider: str
    protocol: str
    payload: object = field(repr=False)
    profile_id: str = ""


@dataclass(frozen=True, slots=True)
class MessageAttachment:
    """Transient event media reference; payload fields are never persisted verbatim."""

    kind: AttachmentKind
    label: str
    segment_index: int = 0
    source: str = "current"
    file: str | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    summary: str | None = field(default=None, repr=False)
    sub_type: str | None = None
    file_size: int | None = None
    emoji_id: str | None = None
    emoji_package_id: str | None = None
    key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SenderIdentity:
    """Platform-neutral sender identity."""

    user_id: str
    nickname: str = ""
    group_card: str = ""
    is_bot: bool = False

    @property
    def display_name(self) -> str:
        """Return the event-provided display name without database fallback."""

        return self.group_card or self.nickname


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Normalized inbound message consumed by policies and chat services."""

    message_id: str
    event_type: str
    scope_type: ScopeType
    sender: SenderIdentity
    text: str
    bot_user_id: str = ""
    raw_text: str = ""
    group_id: str | None = None
    mentions_bot: bool = False
    is_self_message: bool = False
    reply_text: str | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    attachments: tuple[MessageAttachment, ...] = ()
    segments: tuple[dict[str, object], ...] = ()
    reply_attachments: tuple[MessageAttachment, ...] = ()
    reply_segments: tuple[dict[str, object], ...] = ()
    reply_to_message_id: str | None = None
    reply_sender_user_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def scope(self, *, bot_user_id: str | None = None) -> ConversationScope:
        """Build the bot-aware conversation scope for this message."""

        bot = (bot_user_id if bot_user_id is not None else self.bot_user_id).strip()
        if not bot:
            raise ValueError("conversation scope requires bot_user_id")
        if self.scope_type is ScopeType.PRIVATE:
            return ConversationScope.private(bot, self.sender.user_id)
        if self.group_id is None:
            raise ValueError("group message is missing group_id")
        return ConversationScope.group(bot, self.group_id)


@dataclass(frozen=True, slots=True)
class OutboundMedia:
    """Ephemeral outbound media bytes with ledger-safe descriptive metadata."""

    kind: AttachmentKind
    content: bytes = field(default=b"", repr=False)
    mime_type: str = "application/octet-stream"
    summary: str = ""
    emoji_id: str | None = None
    animated: bool = False
    local_path: str | None = field(default=None, repr=False)
    spoken_text: str = field(default="", repr=False)
    generation_id: int | None = None
    voice_profile_id: str | None = None
    voice_reference_key: str | None = None
    voice_language: str | None = None
    duration_milliseconds: int | None = None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Transport-independent text and optional ephemeral media."""

    text: str = ""
    reply_to_message_id: str | None = None
    media: tuple[OutboundMedia, ...] = ()


@dataclass(frozen=True, slots=True)
class OutboundSendReceipt:
    """Proof that one transport accepted an outbound message."""

    platform_message_id: str
    transport: str = "onebot"

    def __post_init__(self) -> None:
        message_id = self.platform_message_id.strip()
        transport = self.transport.strip()
        if not message_id:
            raise ValueError("platform_message_id must not be empty")
        if not transport:
            raise ValueError("transport must not be empty")
        object.__setattr__(self, "platform_message_id", message_id)
        object.__setattr__(self, "transport", transport)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A message sent to a chat completion API, including tool-call turns."""

    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class ToolFunction:
    """A provider-neutral function tool call."""

    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A provider-neutral tool call."""

    id: str
    function: ToolFunction
    type: str = "function"


@dataclass(frozen=True, slots=True)
class ChatTool:
    """A JSON-schema function tool exposed to the model."""

    name: str
    description: str
    parameters: dict[str, object]
    namespace: str = ""
    aliases: tuple[str, ...] = ()
    use_when: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    schema_version: str = "1"


@dataclass(frozen=True, slots=True)
class NativeToolDefinition:
    """One provider-executed tool authorized for this request."""

    type: NativeToolType


@dataclass(frozen=True, slots=True)
class NativeToolEvent:
    """Sanitized event emitted by a provider-executed tool."""

    tool_type: NativeToolType
    call_id: str
    status: NativeToolStatus
    action_type: str = ""
    query: str = field(default="", repr=False)
    url: str = field(default="", repr=False)
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class ResponseCitation:
    """Provider-returned or deterministically recovered public citation."""

    url: str = field(repr=False)
    title: str = ""
    origin: CitationOrigin = CitationOrigin.ANNOTATION
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class FunctionCallOutput:
    """Result of a local Function Tool for a Responses continuation."""

    call_id: str
    output: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PromptRequestDiagnostics:
    """Content-free prompt identity that is never serialized to a provider."""

    conversation_prefix_hash: str
    prompt_snapshot_fingerprint: str
    static_prompt_revision: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Provider-independent chat request."""

    messages: tuple[ChatMessage, ...]
    model: str = ""
    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: ReasoningEffort | None = None
    tools: tuple[ChatTool, ...] = ()
    tool_choice: str | None = None
    response_format: dict[str, object] | None = None
    structured_output: bool = False
    native_tools: tuple[NativeToolDefinition, ...] = ()
    continuation: ProviderContinuation | None = None
    function_outputs: tuple[FunctionCallOutput, ...] = ()
    conversation_prefix_hash: str = ""
    request_shape_hash: str = ""
    prompt_snapshot_fingerprint: str = ""
    static_prompt_revision: str = ""


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Validated provider response."""

    content: str
    latency_seconds: float
    provider_request_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    status: ModelResponseStatus = ModelResponseStatus.COMPLETED
    native_tool_events: tuple[NativeToolEvent, ...] = ()
    citations: tuple[ResponseCitation, ...] = ()
    continuation: ProviderContinuation | None = None
    reasoning_tokens: int | None = None
    incomplete_reason: str | None = None
