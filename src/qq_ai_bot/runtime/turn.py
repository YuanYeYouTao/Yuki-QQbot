"""Turn context (immutable) and turn state (per-turn mutable scratch).

``TurnContext`` is assembled once by the host entry point from trusted
sources; ``normalized_content`` is explicitly wrapped as untrusted and can
never flow into authority decisions.  ``TurnState`` holds only this turn's
mutable working set — it does not duplicate coordinator facts (token /
version / cancellation) nor agent-runner counters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts, TurnTaintState
from qq_ai_bot.runtime.contracts import MemoryReceiptHandle
from qq_ai_bot.runtime.errors import InvalidTurnContextError
from qq_ai_bot.runtime.keys import TurnCoordinationKey
from qq_ai_bot.runtime.trigger import MessageTurnTrigger, TurnTrigger

if TYPE_CHECKING:
    from qq_ai_bot.admin.models import RuntimeConfigSnapshot
    from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.domain.messages import SenderIdentity
    from qq_ai_bot.services.turn_coordinator import TurnToken
    from qq_ai_bot.time.models import TimeContext
    from qq_ai_bot.vision.models import VisualObservation


@dataclass(frozen=True, slots=True)
class UntrustedContent:
    """Model-visible text that must never influence authority or policy.

    Wrapping the normalized content in a distinct type makes it a type error
    to pass user text where trusted host state is expected.
    """

    text: str
    source: str = "user_message"


@dataclass(frozen=True, slots=True)
class ReplyTargetControl:
    """Host-verified reply target for outbound messages.

    ``pinned=True`` means an explicit, backend-verified target (for example
    via a future ``set_reply_target`` tool); ``False`` means heuristic
    default targeting that later stages may replace.
    """

    reply_to_message_id: str | None = None
    pinned: bool = False


@runtime_checkable
class TurnEffect(Protocol):
    """One queued side effect to apply at delivery time.

    Structurally compatible with the existing ``conversation.reply`` effects
    so R4 can enqueue them without a new wrapper type.
    """

    @property
    def kind(self) -> str: ...

    @property
    def source(self) -> str: ...


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Immutable, trusted description of one admitted turn (R1 §4.3)."""

    trigger: TurnTrigger
    authority: TurnAuthority
    scene: TurnSceneFacts
    runtime_config: RuntimeConfigSnapshot
    scope: ConversationScope | None
    actor: SenderIdentity | None
    turn_snapshot: ConversationTurnSnapshot | None
    coordination_key: TurnCoordinationKey | None
    turn_id: str
    turn_token: TurnToken | None
    current_time: TimeContext
    normalized_content: UntrustedContent
    visual_observation: VisualObservation | None = None

    def __post_init__(self) -> None:
        if not self.turn_id:
            raise InvalidTurnContextError("turn context requires an opaque turn id")
        if self.trigger.origin is not self.authority.origin:
            raise InvalidTurnContextError(
                f"trigger origin {self.trigger.origin} does not match "
                f"authority origin {self.authority.origin}"
            )
        if isinstance(self.trigger, MessageTurnTrigger):
            if self.scope is None:
                raise InvalidTurnContextError("message turns require a conversation scope")
            if self.coordination_key is None:
                raise InvalidTurnContextError("message turns require a coordination key")
            if self.actor is None:
                raise InvalidTurnContextError("message turns require an actor")


@dataclass(slots=True)
class TurnState:
    """Mutable working set for one turn — never long-term facts.

    Coordinator owns token/version/cancellation/mutation shield; AgentRunner
    owns model/tool counters and provider continuation; the memory runtime
    owns mutation receipts.  This object only aggregates their *results*.
    """

    taint: TurnTaintState = field(default_factory=TurnTaintState)
    reply_target: ReplyTargetControl | None = None
    effect_queue: list[TurnEffect] = field(default_factory=list)
    declared_tool_schemas: dict[str, str] = field(default_factory=dict)
    callable_capability_revision: int | None = None
    memory_session_id: str | None = None
    memory_receipts: list[MemoryReceiptHandle] = field(default_factory=list)

    def declare_tool_schema(self, tool_name: str, schema_fingerprint: str) -> None:
        """Record a declared tool schema; redeclaration must be identical.

        The declared-schema ledger is monotonic within a turn: once the model
        has seen a schema for ``tool_name``, swapping in a different schema
        mid-turn is a contract violation.
        """

        existing = self.declared_tool_schemas.get(tool_name)
        if existing is not None and existing != schema_fingerprint:
            raise InvalidTurnContextError(
                f"tool {tool_name!r} already declared with a different schema this turn"
            )
        self.declared_tool_schemas[tool_name] = schema_fingerprint
