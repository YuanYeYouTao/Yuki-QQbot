"""Admission contracts for the conversation runtime (R1 protocols).

R1 freezes the decision shapes; the production policies keep living in
``services`` until R4 migrates them.  ``/ai`` commands, rate limiting and
deduplication keep their existing entry points — these contracts only
describe how an admitted-or-not decision is represented.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from qq_ai_bot.runtime.authority import TurnSceneFacts
from qq_ai_bot.runtime.turn import UntrustedContent

if TYPE_CHECKING:
    from qq_ai_bot.admin.models import RuntimeConfigSnapshot
    from qq_ai_bot.domain.messages import InboundMessage


class AdmissionMode(StrEnum):
    """How an inbound message enters the conversation runtime."""

    DIRECT = "direct"
    AUTONOMOUS_CANDIDATE = "autonomous_candidate"
    OBSERVE = "observe"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Outcome of inbound admission.

    ``reason`` must stay low-cardinality (an enum-like string) because it is
    projected into content-free observation rows.
    """

    mode: AdmissionMode
    reason: str = ""

    @property
    def admitted(self) -> bool:
        return self.mode is AdmissionMode.DIRECT


class InboundMessagePolicy(Protocol):
    """Decides whether an inbound message becomes a direct turn."""

    def evaluate(
        self,
        message: InboundMessage,
        *,
        scene: TurnSceneFacts,
        runtime_config: RuntimeConfigSnapshot,
    ) -> AdmissionDecision: ...


@dataclass(frozen=True, slots=True)
class AutonomousCandidate:
    """Trusted inputs for scoring one autonomous-group participation chance."""

    scene: TurnSceneFacts
    latest_content: UntrustedContent
    pending_message_count: int
    bot_recently_active: bool


@dataclass(frozen=True, slots=True)
class AutonomousAdmissionScore:
    """Local heuristic participation score (replaces Planner necessity in R4)."""

    score: float
    threshold: float
    reasons: tuple[str, ...] = ()

    @property
    def admitted(self) -> bool:
        return self.score >= self.threshold


class AutonomousParticipationPolicy(Protocol):
    """Scores autonomous group participation without any model call.

    R4 provides the production heuristic; R1 only freezes the contract shape.
    """

    async def score(self, candidate: AutonomousCandidate) -> AutonomousAdmissionScore: ...
