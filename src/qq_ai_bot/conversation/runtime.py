"""Final conversation runtime protocols (R1 §5).

These protocols serve turns that produce a user-visible reply.  Scheduled
automations, plugin sessions and plugin background turns keep their own
entry points and reuse the lower-level ``TurnRuntimeCore`` instead of faking
user inbound messages.

R1 defines the shapes only; R4 supplies the production implementations.
Protocol members are frozen — later rounds may add keyword-only parameters
with defaults but must not change or remove existing members.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from qq_ai_bot.conversation.state import TurnEffectContext
from qq_ai_bot.runtime.contracts import CapabilityExposureSnapshot
from qq_ai_bot.runtime.result import TurnResult
from qq_ai_bot.runtime.turn import ReplyTargetControl, TurnContext, TurnState

if TYPE_CHECKING:
    from qq_ai_bot.domain.messages import ChatMessage
    from qq_ai_bot.services.agent_runner import AgentRunResult


@dataclass(frozen=True, slots=True)
class PreparedTurn:
    """Everything the agent loop needs, assembled before the first model call.

    Contains no planner plan — context assembly, memory prefetch and initial
    capability exposure are all host decisions.
    """

    model_messages: tuple[ChatMessage, ...]
    memory_session_id: str | None
    capability_exposure: CapabilityExposureSnapshot
    reply_target: ReplyTargetControl
    effect_context: TurnEffectContext


class ConversationTurnSession(Protocol):
    """One admitted reply-producing turn from prepare to close."""

    @property
    def context(self) -> TurnContext: ...

    @property
    def state(self) -> TurnState: ...

    async def prepare(self) -> PreparedTurn: ...

    async def run_agent(self, prepared: PreparedTurn) -> AgentRunResult: ...

    async def deliver(self, result: AgentRunResult) -> TurnResult: ...

    async def close(self) -> None: ...


class ConversationRuntime(Protocol):
    """Entry point for reply-producing turns."""

    async def begin_turn(self, context: TurnContext) -> ConversationTurnSession: ...


class TurnRuntimeCore(Protocol):
    """Lower-level shared core reused by scheduled/plugin turn hosts.

    Runs the bounded agent loop for one prepared turn without owning
    admission or delivery.  R4 provides the production implementation.
    """

    async def execute(self, context: TurnContext, prepared: PreparedTurn) -> AgentRunResult: ...


class TurnRuntimeCoreFactory(Protocol):
    """Builds a ``TurnRuntimeCore`` bound to one turn's trusted context."""

    def create(self, context: TurnContext) -> TurnRuntimeCore: ...
