"""Unit tests for the memory turn contract and capability view (R1 commit 2)."""

from __future__ import annotations

import pytest

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryRecallPurpose
from qq_ai_bot.memory.runtime.capability_view import (
    MEMORY_READ_NAMESPACES,
    MEMORY_WRITE_NAMESPACE,
    build_capability_view,
)
from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryContextPolicy,
    MemoryFinalizationPolicy,
    MemoryReadPolicy,
    MemoryTurnContract,
    MemoryWritePolicy,
    MemoryWriteTransition,
    active_read_contract,
    dormant_contract,
    exclusive_write_contract,
    forbidden_contract,
    passive_contract,
    to_exclusive_write,
    to_locator_read,
    to_mutation_retry,
)
from qq_ai_bot.memory.runtime.resolver import resolve_scope_from_scene
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.origin import TurnOrigin


def _contract(**overrides: object) -> MemoryTurnContract:
    values: dict[str, object] = {
        "context_policy": MemoryContextPolicy.NONE,
        "read_policy": MemoryReadPolicy.DEFERRED,
        "write_policy": MemoryWritePolicy.DISABLED,
        "write_transition": MemoryWriteTransition.REQUESTABLE,
        "finalization_policy": MemoryFinalizationPolicy.NORMAL,
        "availability": MemoryAvailability.ENABLED,
        "default_purpose": MemoryRecallPurpose.BACKGROUND,
    }
    values.update(overrides)
    return MemoryTurnContract(**values)  # type: ignore[arg-type]


class TestContractComposition:
    def test_passive_profile_shape_is_valid(self) -> None:
        contract = _contract()
        assert contract.availability is MemoryAvailability.ENABLED

    def test_background_context_requires_deferred_read(self) -> None:
        contract = _contract(context_policy=MemoryContextPolicy.BACKGROUND)
        assert contract.context_policy is MemoryContextPolicy.BACKGROUND
        with pytest.raises(ValueError, match="requires read=DEFERRED"):
            _contract(
                context_policy=MemoryContextPolicy.BACKGROUND,
                read_policy=MemoryReadPolicy.EAGER,
            )
        with pytest.raises(ValueError, match="requires read=DEFERRED"):
            _contract(
                context_policy=MemoryContextPolicy.CONTINUATION,
                read_policy=MemoryReadPolicy.DENIED,
            )

    def test_forbidden_requires_fully_denied_shape(self) -> None:
        contract = forbidden_contract(MemoryRecallPurpose.BACKGROUND)
        assert contract.availability is MemoryAvailability.FORBIDDEN
        with pytest.raises(ValueError, match="FORBIDDEN availability"):
            _contract(
                availability=MemoryAvailability.FORBIDDEN,
                read_policy=MemoryReadPolicy.DEFERRED,
                write_transition=MemoryWriteTransition.DENIED,
            )

    def test_exclusive_write_shape_is_all_or_nothing(self) -> None:
        exclusive = _contract(
            read_policy=MemoryReadPolicy.DENIED,
            write_policy=MemoryWritePolicy.EXCLUSIVE,
            write_transition=MemoryWriteTransition.ALREADY_EXCLUSIVE,
            finalization_policy=MemoryFinalizationPolicy.RECEIPT_GATED,
        )
        assert exclusive.write_policy is MemoryWritePolicy.EXCLUSIVE

        with pytest.raises(ValueError, match="forbids automatic context"):
            _contract(
                context_policy=MemoryContextPolicy.BACKGROUND,
                read_policy=MemoryReadPolicy.DEFERRED,
                write_policy=MemoryWritePolicy.EXCLUSIVE,
                write_transition=MemoryWriteTransition.ALREADY_EXCLUSIVE,
                finalization_policy=MemoryFinalizationPolicy.RECEIPT_GATED,
            )
        with pytest.raises(ValueError, match="transition=ALREADY_EXCLUSIVE"):
            _contract(
                read_policy=MemoryReadPolicy.DENIED,
                write_policy=MemoryWritePolicy.EXCLUSIVE,
                write_transition=MemoryWriteTransition.REQUESTABLE,
                finalization_policy=MemoryFinalizationPolicy.RECEIPT_GATED,
            )
        with pytest.raises(ValueError, match="finalization=RECEIPT_GATED"):
            _contract(
                read_policy=MemoryReadPolicy.DENIED,
                write_policy=MemoryWritePolicy.EXCLUSIVE,
                write_transition=MemoryWriteTransition.ALREADY_EXCLUSIVE,
                finalization_policy=MemoryFinalizationPolicy.NORMAL,
            )

    def test_reverse_exclusive_markers_require_exclusive_write(self) -> None:
        with pytest.raises(ValueError, match="requires write=EXCLUSIVE"):
            _contract(write_transition=MemoryWriteTransition.ALREADY_EXCLUSIVE)
        with pytest.raises(ValueError, match="requires write=EXCLUSIVE"):
            _contract(finalization_policy=MemoryFinalizationPolicy.RECEIPT_GATED)

    def test_locator_only_read_needs_mutation_lane(self) -> None:
        locator = _contract(
            read_policy=MemoryReadPolicy.LOCATOR_ONLY,
            write_policy=MemoryWritePolicy.EXCLUSIVE,
            write_transition=MemoryWriteTransition.ALREADY_EXCLUSIVE,
            finalization_policy=MemoryFinalizationPolicy.RECEIPT_GATED,
        )
        assert locator.read_policy is MemoryReadPolicy.LOCATOR_ONLY
        with pytest.raises(ValueError, match="mutation locator phase"):
            _contract(read_policy=MemoryReadPolicy.LOCATOR_ONLY)

    def test_contract_is_frozen(self) -> None:
        contract = _contract()
        with pytest.raises(Exception, match="frozen"):
            contract.read_policy = MemoryReadPolicy.EAGER  # type: ignore[misc]


class TestWriteAuthority:
    def test_requestable_rejected_without_persistent_write_authority(self) -> None:
        contract = _contract()
        contract.require_write_authority(persistent_write_allowed=True)
        with pytest.raises(ValueError, match="persistent memory writes"):
            contract.require_write_authority(persistent_write_allowed=False)

    def test_read_only_contract_needs_no_write_authority(self) -> None:
        contract = _contract(write_transition=MemoryWriteTransition.DENIED)
        contract.require_write_authority(persistent_write_allowed=False)


class TestCapabilityView:
    def test_forbidden_hides_everything(self) -> None:
        view = build_capability_view(
            forbidden_contract(MemoryRecallPurpose.BACKGROUND), transition_revision=1
        )
        assert view.eager_namespaces == ()
        assert view.requestable_namespaces == ()
        assert set(view.hidden_namespaces) == {*MEMORY_READ_NAMESPACES, MEMORY_WRITE_NAMESPACE}
        assert view.exclusive_namespace is None

    def test_passive_maps_to_requestable(self) -> None:
        view = build_capability_view(_contract(), transition_revision=2)
        assert view.eager_namespaces == ()
        assert set(view.requestable_namespaces) == {
            *MEMORY_READ_NAMESPACES,
            MEMORY_WRITE_NAMESPACE,
        }
        assert view.transition_revision == 2

    def test_active_read_maps_to_eager(self) -> None:
        view = build_capability_view(
            _contract(read_policy=MemoryReadPolicy.EAGER), transition_revision=3
        )
        assert set(view.eager_namespaces) == set(MEMORY_READ_NAMESPACES)
        assert view.requestable_namespaces == (MEMORY_WRITE_NAMESPACE,)

    def test_locator_only_exposes_read_namespaces_on_exclusive_lane(self) -> None:
        view = build_capability_view(
            exclusive_write_contract(locator_only=True),
            transition_revision=5,
        )
        assert MEMORY_WRITE_NAMESPACE in view.eager_namespaces
        assert set(MEMORY_READ_NAMESPACES) <= set(view.eager_namespaces)
        assert view.exclusive_namespace == MEMORY_WRITE_NAMESPACE
        assert view.hidden_namespaces == ()

    def test_exclusive_write_exposes_only_write_namespace(self) -> None:
        view = build_capability_view(
            _contract(
                read_policy=MemoryReadPolicy.DENIED,
                write_policy=MemoryWritePolicy.EXCLUSIVE,
                write_transition=MemoryWriteTransition.ALREADY_EXCLUSIVE,
                finalization_policy=MemoryFinalizationPolicy.RECEIPT_GATED,
            ),
            transition_revision=4,
        )
        assert view.eager_namespaces == (MEMORY_WRITE_NAMESPACE,)
        assert view.exclusive_namespace == MEMORY_WRITE_NAMESPACE
        assert set(view.hidden_namespaces) == set(MEMORY_READ_NAMESPACES)


class TestProfileFactories:
    def test_factories_are_valid_shapes_not_checkable_names(self) -> None:
        dormant = dormant_contract()
        passive = passive_contract()
        continuation = passive_contract(MemoryRecallPurpose.CONTINUATION)
        active = active_read_contract()
        exclusive = exclusive_write_contract()
        assert dormant.context_policy is MemoryContextPolicy.NONE
        assert dormant.read_policy is MemoryReadPolicy.DEFERRED
        assert passive.context_policy is MemoryContextPolicy.BACKGROUND
        assert continuation.context_policy is MemoryContextPolicy.CONTINUATION
        assert active.read_policy is MemoryReadPolicy.EAGER
        assert exclusive.write_policy is MemoryWritePolicy.EXCLUSIVE
        assert exclusive.finalization_policy is MemoryFinalizationPolicy.RECEIPT_GATED

    def test_image_or_external_write_denial_uses_transition_not_profile(self) -> None:
        contract = passive_contract(persistent_write_allowed=False)
        assert contract.write_transition is MemoryWriteTransition.DENIED
        assert contract.context_policy is MemoryContextPolicy.BACKGROUND

    def test_passive_rejects_non_automatic_purpose(self) -> None:
        with pytest.raises(ValueError, match="background or continuation"):
            passive_contract(MemoryRecallPurpose.RECALL)

    def test_requestable_to_exclusive_and_locator_round_trip(self) -> None:
        start = passive_contract()
        exclusive = to_exclusive_write(start)
        assert exclusive.write_policy is MemoryWritePolicy.EXCLUSIVE
        locator = to_locator_read(exclusive)
        assert locator.read_policy is MemoryReadPolicy.LOCATOR_ONLY
        retry = to_mutation_retry(locator)
        assert retry.read_policy is MemoryReadPolicy.DENIED
        assert retry.write_policy is MemoryWritePolicy.EXCLUSIVE

    def test_forbidden_cannot_enter_exclusive_write(self) -> None:
        with pytest.raises(ValueError, match="FORBIDDEN"):
            to_exclusive_write(forbidden_contract(MemoryRecallPurpose.BACKGROUND))


class TestScopeResolution:
    def _authority(self, origin: TurnOrigin = TurnOrigin.USER_MESSAGE) -> TurnAuthority:
        return TurnAuthority(
            actor_user_id="u-1",
            bot_user_id="bot-9",
            origin=origin,
            permission_ceiling=frozenset({"user"}),
            delegated_authority=None,
            authority_revision=1,
        )

    def test_group_scene_resolves_group_partition(self) -> None:
        scope = resolve_scope_from_scene(
            authority=self._authority(),
            scene=TurnSceneFacts(scope_type=ScopeType.GROUP, group_id="g-7"),
        )
        assert scope.partition_key == "group:g-7"

    def test_private_scene_resolves_trusted_actor_partition(self) -> None:
        scope = resolve_scope_from_scene(
            authority=self._authority(),
            scene=TurnSceneFacts(scope_type=ScopeType.PRIVATE, group_id=None),
        )
        assert scope.partition_key == "private:u-1"
