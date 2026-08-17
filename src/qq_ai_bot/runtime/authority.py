"""Trusted per-turn authority, scene facts and taint state.

``TurnAuthority`` is built exclusively by host factories from trusted inputs
(settings, coordinator, ledger).  Model output must never flow into any field
of this module.

``DelegatedAuthoritySnapshot`` is the runtime-neutral mirror of
``automation.authority.DelegatedAuthority``: the runtime layer must not import
the automation domain (it drags in ``Settings`` and the capability registry),
so delegation is revalidated here through a pure function fed with
pre-extracted facts.  ``revalidate_delegated_capabilities`` replicates the
semantics of ``automation.authority.effective_delegated_capabilities``
(superuser downgrade to empty set, schema version equality, provenance triple
equality, current permission, allowed origin).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.runtime.errors import InvalidTurnContextError
from qq_ai_bot.runtime.origin import TurnOrigin


class DelegationPermissionLevel(StrEnum):
    """Permission level recorded when authority was delegated.

    Values match ``automation.authority.PermissionLevel`` so conversion is a
    plain value copy.
    """

    USER = "user"
    SUPERUSER = "superuser"


@dataclass(frozen=True, slots=True)
class DelegatedAuthoritySnapshot:
    """Neutral, immutable record of a delegated authority grant.

    Grants are captured at creation time and must be revalidated against
    current facts before every use; the snapshot itself never expands.
    """

    creator_user_id: str
    bot_user_id: str
    created_from_message_id: str
    created_at: str
    permission_level: DelegationPermissionLevel
    granted_capabilities: tuple[str, ...]
    capability_schema_versions: Mapping[str, int | str]
    capability_provenance: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    authority_version: int = 1
    origin: TurnOrigin = TurnOrigin.SCHEDULED_AUTOMATION
    current_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityRevalidationFacts:
    """Current facts about one capability, extracted by the owning domain.

    The automation/plugin layer converts its registry entry into this neutral
    shape; the pure revalidation below never touches the registry itself.
    """

    schema_version: int | str
    provenance: Mapping[str, str] | None
    permitted_for_creator: bool
    allowed_for_origin: bool


def revalidate_delegated_capabilities(
    snapshot: DelegatedAuthoritySnapshot,
    *,
    creator_is_currently_superuser: bool,
    facts: Mapping[str, CapabilityRevalidationFacts],
) -> frozenset[str]:
    """Intersect the immutable grant with current facts.

    Mirrors ``automation.authority.effective_delegated_capabilities``:

    - a superuser-level grant collapses to the empty set the moment the
      creator loses superuser status;
    - capabilities missing from ``facts`` (unregistered) are dropped;
    - schema version and, for plugin-provided capabilities, the provenance
      triple must match exactly;
    - the capability must still be permitted for the creator's *current*
      permission level and allowed for the delegated origin.
    """

    if (
        snapshot.permission_level is DelegationPermissionLevel.SUPERUSER
        and not creator_is_currently_superuser
    ):
        return frozenset()
    allowed: set[str] = set()
    for name in snapshot.granted_capabilities:
        fact = facts.get(name)
        if fact is None:
            continue
        if snapshot.capability_schema_versions.get(name) != fact.schema_version:
            continue
        if fact.provenance is not None:
            expected = dict(snapshot.capability_provenance.get(name, {}))
            if expected != dict(fact.provenance):
                continue
        if not fact.permitted_for_creator:
            continue
        if not fact.allowed_for_origin:
            continue
        allowed.add(name)
    return frozenset(allowed)


@dataclass(frozen=True, slots=True)
class TurnAuthority:
    """Immutable authority envelope for one turn, built by a host factory.

    ``permission_ceiling`` is the maximum capability surface the actor may
    ever reach this turn; later stages may only narrow it (see
    :func:`effective_capability_set`), never widen it.
    """

    actor_user_id: str
    bot_user_id: str
    origin: TurnOrigin
    permission_ceiling: frozenset[str]
    delegated_authority: DelegatedAuthoritySnapshot | None
    authority_revision: int

    def __post_init__(self) -> None:
        if not self.actor_user_id:
            raise InvalidTurnContextError("turn authority requires an actor user id")
        if not self.bot_user_id:
            raise InvalidTurnContextError("turn authority requires the bot user id")
        if self.authority_revision < 1:
            raise InvalidTurnContextError("authority revision must be >= 1")


def effective_capability_set(
    *,
    permission_ceiling: frozenset[str],
    current_permission: frozenset[str],
    scene_allowed: frozenset[str],
    delegated: frozenset[str] | None = None,
) -> frozenset[str]:
    """Pure narrowing intersection; ``delegated=None`` means no delegation.

    The result can never exceed ``permission_ceiling`` — this is the single
    place where the "authority may only narrow" invariant is computed.
    """

    allowed = permission_ceiling & current_permission & scene_allowed
    if delegated is not None:
        allowed &= delegated
    return allowed


@dataclass(frozen=True, slots=True)
class TurnSceneFacts:
    """Trusted, host-observed facts about the scene of one turn."""

    scope_type: ScopeType
    group_id: str | None
    image_present: bool = False
    mentions_bot: bool = False
    replies_to_bot: bool = False
    reply_present: bool = False

    def __post_init__(self) -> None:
        if self.scope_type is ScopeType.GROUP and not self.group_id:
            raise InvalidTurnContextError("group scene requires a group id")
        if self.scope_type is ScopeType.PRIVATE and self.group_id is not None:
            raise InvalidTurnContextError("private scene must not carry a group id")


class TurnTaintState:
    """Monotonic taint flags for one turn.

    Flags can only be raised, never cleared: once external data entered the
    model context, or a durable mutation committed, the rest of the turn must
    behave accordingly.
    """

    __slots__ = ("_external_data_consumed", "_mutation_committed")

    def __init__(self) -> None:
        self._external_data_consumed = False
        self._mutation_committed = False

    @property
    def external_data_consumed(self) -> bool:
        return self._external_data_consumed

    @property
    def mutation_committed(self) -> bool:
        return self._mutation_committed

    def mark_external_data_consumed(self) -> None:
        self._external_data_consumed = True

    def mark_mutation_committed(self) -> None:
        self._mutation_committed = True

    def __repr__(self) -> str:
        return (
            "TurnTaintState("
            f"external_data_consumed={self._external_data_consumed}, "
            f"mutation_committed={self._mutation_committed})"
        )
