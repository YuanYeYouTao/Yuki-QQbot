"""V6 host-owned mode selection; never an Agent execution or authorization version.

The Host selector persists these contracts before dispatch through the shared Work runtime.
None of these values grants tool, memory, execution or send authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal


class AutonomyOwner(StrEnum):
    OFF = "off"
    LEGACY = "legacy"
    SEMANTIC = "semantic"


class InitiativeSourceKind(StrEnum):
    EVENT = "event"
    MEMORY = "memory"


@dataclass(frozen=True, slots=True, order=True)
class InitiativeSource:
    """A host-resolved focus source, not a speaker or an authorization token.

    Context-only references must not be passed as focus sources. The revision is an
    opaque host version, so rereading unchanged content cannot make a new source.
    """

    kind: InitiativeSourceKind
    source_id: str
    revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, InitiativeSourceKind):
            raise ValueError("initiative_source_kind_invalid")
        if (
            not self.source_id.isascii()
            or not self.source_id.isdecimal()
            or self.source_id.startswith("0")
            or len(self.source_id) > 20
        ):
            raise ValueError("initiative_source_requires_internal_id")
        if not self.revision or self.revision != self.revision.strip() or len(self.revision) > 128:
            raise ValueError("initiative_source_revision_invalid")


@dataclass(frozen=True, slots=True)
class AutonomyBinding:
    conversation_id: str
    generation: int
    master_enabled: bool = False
    external_enabled: bool = False
    effective_owner: AutonomyOwner = AutonomyOwner.OFF
    controller_epoch: int = 0
    fallback_reason: str | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not self.conversation_id or self.generation < 1 or self.revision < 1:
            raise ValueError("autonomy_binding_identity_invalid")
        if self.controller_epoch < 0:
            raise ValueError("autonomy_binding_epoch_invalid")

    def transition(
        self,
        *,
        master_enabled: bool,
        external_enabled: bool,
    ) -> AutonomyBinding:
        """Explicit policy selects the proposer; observer health never changes ownership."""
        owner = (
            AutonomyOwner.OFF
            if not master_enabled
            else AutonomyOwner.SEMANTIC
            if external_enabled
            else AutonomyOwner.LEGACY
        )
        changed = (
            master_enabled != self.master_enabled
            or external_enabled != self.external_enabled
            or owner != self.effective_owner
        )
        return replace(
            self,
            master_enabled=master_enabled,
            external_enabled=external_enabled,
            effective_owner=owner,
            controller_epoch=self.controller_epoch + int(changed),
            fallback_reason=None,
            revision=self.revision + int(changed or self.fallback_reason is not None),
        )

    def accepts(
        self,
        *,
        owner: AutonomyOwner,
        epoch: int,
        conversation_id: str,
        generation: int,
    ) -> bool:
        """Only checks proposal ownership; permissions/source/run occupancy remain host checks."""
        return (
            self.master_enabled
            and owner is not AutonomyOwner.OFF
            and owner is self.effective_owner
            and epoch == self.controller_epoch
            and conversation_id == self.conversation_id
            and generation == self.generation
        )


@dataclass(frozen=True, slots=True)
class AcceptedInitiative:
    """Persistent admission contract: accepted execution is independent of later mode switches.

    No actor_user_id, person profile, platform message ID or permissions borrowed from a
    source event. This does not grant capabilities; integration needs a true SELF principal.
    """

    run_id: str
    proposal_id: str
    conversation_id: str
    generation: int
    space_id: str
    presence_id: str
    controller_epoch_at_acceptance: int
    sources: tuple[InitiativeSource, ...]
    owner: AutonomyOwner
    target_person_id: str | None = None
    support_refs: tuple[str, ...] = ()
    state: str = "accepted"
    feedback_sequence: int = 0
    trigger_kind: Literal["source", "intrinsic"] = "source"
    thread_key: str | None = None

    def __post_init__(self) -> None:
        if not all(
            (self.run_id, self.proposal_id, self.conversation_id, self.space_id, self.presence_id)
        ):
            raise ValueError("initiative_requires_host_identity")
        if self.generation < 1 or self.controller_epoch_at_acceptance < 0:
            raise ValueError("initiative_requires_nonnegative_version")
        if len(self.sources) > 32 or len(set(self.sources)) != len(self.sources):
            raise ValueError("initiative_requires_distinct_bounded_sources")
        if self.trigger_kind == "source" and not self.sources:
            raise ValueError("initiative_requires_distinct_bounded_sources")
        if self.trigger_kind == "intrinsic" and (
            self.sources
            or self.support_refs
            or self.target_person_id is not None
            or self.owner is not AutonomyOwner.SEMANTIC
        ):
            raise ValueError("intrinsic_initiative_requires_self_group")
        if self.trigger_kind not in {"source", "intrinsic"}:
            raise ValueError("initiative_trigger_kind_invalid")
        if self.thread_key is not None and (not self.thread_key or len(self.thread_key) > 256):
            raise ValueError("initiative_thread_key_invalid")
        if self.owner not in {AutonomyOwner.LEGACY, AutonomyOwner.SEMANTIC}:
            raise ValueError("initiative_requires_controller_owner")

    def belongs_to(self, conversation_id: str, generation: int) -> bool:
        # Deliberately no controller epoch: changing the proposer must not cancel work.
        return self.conversation_id == conversation_id and self.generation == generation
